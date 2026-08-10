"""Launches and keeps alive app-class extensions.

The first thing in this project to own a child process. It mirrors the
supervision already proven for adapter watch tasks
(``AdapterManager._supervised_watch``): a per-app task, exponential backoff on
failure, a health reset once a run has lasted, and ``CancelledError`` re-raised
first so shutdown never deadlocks.

One deliberate divergence: for an adapter watch a clean return means the work
is finished, so the loop exits. For a supervised process a clean exit is *not*
terminal - a client that quits on its own while the user still wants it running
should come back. The loop ends only when the user says stop.

``start()`` never raises. ``Server.start()`` has no exception handling and sits
outside the ``try/finally`` in ``run()``, so a service that throws there kills
the process and leaks every task started before it. A missing client executable
must be a reported state, not a boot failure.
"""

import asyncio
import builtins
import contextlib
import json
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel

from prodeo.apps.manifest import AppManifest, config_to_env, installed_apps
from prodeo.errors import AppNotReadyError, UnknownAppError
from prodeo.events import Event, new_event
from prodeo.events import types as ev

_log = structlog.get_logger(__name__)

_SOURCE = "app-supervisor"

#: Grace given to a child to exit before it is killed outright.
TERMINATE_TIMEOUT_S = 5.0

#: Backoff bounds for restarting a crashed child, matching AdapterManager.
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0
#: A run lasting longer than this counts as healthy and resets the backoff.
_HEALTHY_RUN_S = 60.0
#: Consecutive failures, none of which ever ran long enough to look healthy,
#: after which the supervisor stops trying. A crash on startup is nearly always
#: misconfiguration, and that will not fix itself: retrying forever only writes
#: two events a minute into the durable log until someone notices.
_MAX_CRASH_LOOP = 5

AppState = Literal["stopped", "starting", "running", "exited", "failed"]

#: Injected rather than imported - services never import each other.
PublishFn = Callable[[Event], Awaitable[None]]
ConfigFn = Callable[[str], Awaitable[dict[str, Any]]]
AutostartFn = Callable[[], Awaitable[list[str]]]
SpawnFn = Callable[[AppManifest, dict[str, str]], Awaitable[asyncio.subprocess.Process]]


class SetupGap(Protocol):
    """One unmet setup step, as the readiness probe reports it.

    Structurally matches :class:`prodeo.extensions.AppSetupGap` without
    importing it - services never import each other, and this seam is how the
    supervisor stays ignorant of what a catalog or an asset is.
    """

    @property
    def description(self) -> str: ...

    @property
    def config_pointer(self) -> str: ...


#: The readiness probe: given an app name, the setup steps it still needs.
SetupGapsFn = Callable[[str], Awaitable[Sequence[SetupGap]]]


class PresenceView(Protocol):
    """The slice of the presence tracker the supervisor needs."""

    def list_clients(self) -> Sequence[Any]: ...

    def forget(self, client_id: str) -> bool: ...


class AppStatus(BaseModel):
    """One app extension as the dashboard sees it."""

    name: str
    version: str = ""
    description: str = ""
    state: AppState = "stopped"
    pid: int | None = None
    started_at: datetime | None = None
    #: How many times the supervisor has restarted it since the server booted.
    restarts: int = 0
    #: Why it is not running, when it is not.
    last_error: str = ""
    #: Launch with the server. Off by default - a process that listens to a
    #: microphone should not start itself uninvited.
    autostart: bool = False
    #: The client has heartbeated recently. Distinguishes "the process is up"
    #: from "the client is actually working".
    present: bool = False
    configurable: bool = False
    #: Setup steps still needed before this app can start, in human words.
    #: Filled by the API layer at query time (the check is async and the
    #: supervisor's queries are not); non-empty means Start will be refused.
    unmet_setup: list[str] = []


class _Running:
    """Mutable per-app state the supervisor keeps."""

    def __init__(self) -> None:
        self.wanted = False
        self.state: AppState = "stopped"
        self.pid: int | None = None
        self.started_at: datetime | None = None
        self.restarts = 0
        self.last_error = ""
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task[None] | None = None


class AppSupervisor:
    """Owns the lifecycle of every installed app extension."""

    def __init__(
        self,
        *,
        node: str = "local",
        publish: PublishFn | None = None,
        presence: PresenceView | None = None,
        config_fn: ConfigFn | None = None,
        autostart_fn: AutostartFn | None = None,
        server_url_fn: Callable[[], str] | None = None,
        api_token: str | None = None,
        manifests_fn: Callable[[], list[AppManifest]] = installed_apps,
        spawn_fn: SpawnFn | None = None,
        setup_gaps_fn: SetupGapsFn | None = None,
    ) -> None:
        self._node = node
        self._publish = publish
        self._presence = presence
        # Callables, not values: the composition root wires this before the
        # API is listening, and the resolved port is only known afterwards.
        self._config_fn = config_fn
        self._autostart_fn = autostart_fn
        self._server_url_fn = server_url_fn
        self._api_token = api_token
        self._manifests_fn = manifests_fn
        self._setup_gaps_fn = setup_gaps_fn
        self._spawn_fn: SpawnFn = spawn_fn or self._spawn
        self._apps: dict[str, AppManifest] = {}
        self._state: dict[str, _Running] = {}
        self._autostart: set[str] = set()
        self._stopping = False

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Discover apps and launch the ones marked autostart. Never raises."""
        try:
            self._apps = {m.name: m for m in self._manifests_fn()}
            for name in self._apps:
                self._state.setdefault(name, _Running())
            self._autostart = set(await self._autostart_fn()) if self._autostart_fn else set()
            _log.info("apps.started", installed=len(self._apps), autostart=len(self._autostart))
            for name in sorted(self._autostart):
                if name not in self._apps:
                    _log.warning("apps.autostart_not_installed", app=name)
                    continue
                try:
                    await self.start_app(name)
                except AppNotReadyError as exc:
                    # Setup was undone since autostart was chosen (a wiped
                    # models drive, a hand-edited config). Report instead of
                    # crash-looping into the durable log at every backoff.
                    state = self._state.setdefault(name, _Running())
                    state.state = "failed"
                    state.last_error = str(exc)
                    _log.warning("apps.autostart_not_ready", app=name, gaps=exc.gaps)
        except Exception:
            # A supervisor that cannot start must not take the server with it.
            _log.exception("apps.start_failed")

    async def stop(self) -> None:
        self._stopping = True
        for state in self._state.values():
            state.wanted = False
            if state.task is not None:
                state.task.cancel()
        for state in self._state.values():
            if state.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await state.task
                state.task = None
        for name in list(self._state):
            await self._terminate(name)

    # --- commands ---------------------------------------------------------

    async def start_app(self, name: str) -> AppStatus:
        manifest = self._manifest(name)
        state = self._state.setdefault(name, _Running())
        if state.task is not None and not state.task.done():
            return self.status_of(name)
        gaps = await self.unmet_setup(name)
        if gaps:
            # Starting anyway is how "exited with code 1" reaches a user with
            # no idea what it means: the child would crash-loop on missing
            # config until the crash-loop guard gives up (ADR-0017).
            raise AppNotReadyError(name, gaps)
        state.wanted = True
        state.last_error = ""
        state.state = "starting"
        state.task = asyncio.create_task(self._supervise(manifest), name=f"app:{name}")
        return self.status_of(name)

    async def stop_app(self, name: str) -> AppStatus:
        self._manifest(name)
        state = self._state.setdefault(name, _Running())
        state.wanted = False
        if state.task is not None:
            state.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.task
            state.task = None
        await self._terminate(name)
        state.state = "stopped"
        return self.status_of(name)

    async def restart_app(self, name: str) -> AppStatus:
        await self.stop_app(name)
        return await self.start_app(name)

    # --- queries ----------------------------------------------------------

    def list(self) -> list[AppStatus]:
        return [self.status_of(name) for name in sorted(self._apps)]

    def status_of(self, name: str) -> AppStatus:
        manifest = self._manifest(name)
        state = self._state.setdefault(name, _Running())
        return AppStatus(
            name=name,
            version=manifest.version,
            description=manifest.description,
            state=state.state,
            pid=state.pid,
            started_at=state.started_at,
            restarts=state.restarts,
            last_error=state.last_error,
            autostart=name in self._autostart,
            present=self._is_present(manifest),
            configurable=manifest.config_model is not None,
        )

    def manifest_of(self, name: str) -> AppManifest:
        return self._manifest(name)

    # builtins.list: the ``list`` method above shadows the builtin here.
    async def unmet_setup(self, name: str) -> builtins.list[str]:
        """Setup steps this app still needs; non-empty means start is refused.

        The probe reads *saved* config, but an app configured through the
        server's environment is just as startable: the child inherits
        ``os.environ`` (see :meth:`_child_env`), so a gap whose value the
        environment provides is not a gap. Errors in the probe itself fail
        open - a broken catalog must degrade to the old behaviour (start
        allowed, crash-loop guard as backstop), never make apps unstartable.
        """
        manifest = self._manifest(name)
        if self._setup_gaps_fn is None:
            return []
        try:
            gaps = await self._setup_gaps_fn(name)
        except Exception:
            _log.exception("apps.setup_probe_failed", app=name)
            return []
        return [
            gap.description
            for gap in gaps
            if not _env_provides(manifest.env_prefix, gap.config_pointer)
        ]

    def set_autostart(self, name: str, enabled: bool) -> None:
        """Record the stored intention so ``status_of`` reports it truthfully."""
        self._manifest(name)
        if enabled:
            self._autostart.add(name)
        else:
            self._autostart.discard(name)

    # --- internals --------------------------------------------------------

    def _manifest(self, name: str) -> AppManifest:
        manifest = self._apps.get(name)
        if manifest is None:
            raise UnknownAppError(f"app extension {name!r} is not installed")
        return manifest

    def _is_present(self, manifest: AppManifest) -> bool:
        if not manifest.presence_client_id or self._presence is None:
            return False
        return any(
            getattr(c, "client_id", None) == manifest.presence_client_id
            for c in self._presence.list_clients()
        )

    async def _supervise(self, manifest: AppManifest) -> None:
        """Keep one app running while the user wants it running."""
        name = manifest.name
        state = self._state[name]
        backoff = _BACKOFF_START
        crash_loop = 0
        loop = asyncio.get_running_loop()
        while state.wanted and not self._stopping:
            started = loop.time()
            try:
                process = await self._launch(manifest)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Almost always a missing executable. Report it and stop: a
                # retry loop will not conjure a binary onto the machine.
                _log.exception("apps.launch_failed", app=name)
                state.state = "failed"
                state.last_error = str(exc)
                state.wanted = False
                await self._emit(ev.SYSTEM_APP_EXITED, {"app": name, "error": str(exc)})
                return

            code = await process.wait()
            state.pid = None
            state.process = None
            if not state.wanted or self._stopping:
                state.state = "stopped"
                return

            healthy = loop.time() - started > _HEALTHY_RUN_S
            crash_loop = 0 if healthy else crash_loop + 1
            if crash_loop >= _MAX_CRASH_LOOP:
                # It has never managed to stay up. Stop rather than keep
                # writing a pair of events into the log every minute forever.
                state.state = "failed"
                state.last_error = (
                    f"exited with code {code} on {crash_loop} consecutive starts; "
                    "giving up - check the app's configuration"
                )
                state.wanted = False
                _log.error("apps.crash_looping", app=name, attempts=crash_loop, code=code)
                await self._emit(
                    ev.SYSTEM_APP_EXITED, {"app": name, "code": code, "restarting": False}
                )
                return

            # A clean exit is NOT terminal here, unlike an adapter watch: the
            # user still wants this running, so bring it back.
            state.state = "exited"
            state.last_error = f"exited with code {code}"
            state.restarts += 1
            await self._emit(ev.SYSTEM_APP_EXITED, {"app": name, "code": code, "restarting": True})
            if healthy:
                backoff = _BACKOFF_START  # it ran fine for a while; retry promptly
            _log.warning("apps.exited", app=name, code=code, backoff_s=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _launch(self, manifest: AppManifest) -> asyncio.subprocess.Process:
        state = self._state[manifest.name]
        process = await self._spawn_fn(manifest, await self._child_env(manifest))
        state.process = process
        state.pid = process.pid
        state.started_at = datetime.now(UTC)
        state.state = "running"
        _log.info("apps.launched", app=manifest.name, pid=process.pid)
        await self._emit(ev.SYSTEM_APP_STARTED, {"app": manifest.name, "pid": process.pid})
        return process

    async def _child_env(self, manifest: AppManifest) -> dict[str, str]:
        """The child's full environment: ours, plus its rendered config."""
        config = await self._config_fn(manifest.name) if self._config_fn else {}
        # Fill in where the server is, so a locally supervised client is never
        # asked to be told its own server's address. The app names the fields;
        # core does not assume them.
        if manifest.server_url_field and self._server_url_fn is not None:
            config.setdefault(manifest.server_url_field, self._server_url_fn())
        if manifest.api_token_field and self._api_token:
            config.setdefault(manifest.api_token_field, self._api_token)
        return {**os.environ, **config_to_env(manifest, config)}

    async def _spawn(
        self, manifest: AppManifest, env: dict[str, str]
    ) -> asyncio.subprocess.Process:
        argv = list(manifest.command)
        if not argv:
            raise RuntimeError(f"{manifest.name!r} declares no command to run")
        argv[0] = _resolve_executable(argv[0])
        return await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _terminate(self, name: str) -> None:
        state = self._state.get(name)
        if state is None or state.process is None:
            return
        process = state.process
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            async with asyncio.timeout(TERMINATE_TIMEOUT_S):
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        state.process = None
        state.pid = None
        state.state = "stopped"
        # On Windows terminate() is a hard kill, so the client never gets to
        # send its presence goodbye and would linger for its whole TTL. Clear
        # it here, or the dashboard shows a client that is definitely gone.
        manifest = self._apps.get(name)
        if manifest and manifest.presence_client_id and self._presence is not None:
            self._presence.forget(manifest.presence_client_id)

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._publish is not None:
            await self._publish(
                new_event(event_type, node=self._node, source=_SOURCE, payload=payload)
            )


def _resolve_executable(name: str) -> str:
    """Find a console script on PATH, falling back to this venv's scripts.

    A server launched through ``uv run`` has its venv on PATH, but one started
    another way may not - and the client is almost always installed beside the
    server.
    """
    found = shutil.which(name)
    if found:
        return found
    scripts = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")
    for candidate in (scripts / name, scripts / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(f"{name!r} is not installed or not on PATH")


def _env_provides(env_prefix: str, pointer: str) -> bool:
    """Whether the server's environment already supplies this config value.

    ``engines.piper.voice_path`` under prefix ``MJOLNIR_`` is provided when
    ``MJOLNIR_ENGINES`` exists and its JSON contains ``piper.voice_path`` -
    the same rendering :func:`config_to_env` uses, read in reverse. An env var
    whose JSON does not parse gets the benefit of the doubt: whoever hand-set
    it on the server process is past the wizard's audience, and a wrong guess
    here must not make their app unstartable.
    """
    if not env_prefix or not pointer:
        return False
    field, _, rest = pointer.partition(".")
    raw = os.environ.get(f"{env_prefix}{field}".upper())
    if raw is None:
        return False
    if not rest:
        return True
    try:
        node: Any = json.loads(raw)
    except ValueError:
        return True
    for part in rest.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True
