"""Supervisor behaviour: launch, restart, stop, containment, presence.

Processes are faked through the ``spawn_fn`` seam, so nothing here spawns a
real child; the supervision logic is what is under test.
"""

import asyncio
from typing import Any

import pytest

from prodeo.apps import AppManifest, AppSupervisor
from prodeo.apps import supervisor as supervisor_module
from prodeo.errors import UnknownAppError
from prodeo.events import Event


class FakeProcess:
    """A process whose exit the test controls."""

    _next_pid = 1000

    def __init__(self, exit_code: int = 0) -> None:
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.exit_code

    def finish(self, code: int | None = None) -> None:
        if code is not None:
            self.exit_code = code
        self._exited.set()

    def terminate(self) -> None:
        self.terminated = True
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self._exited.set()


class FakeSpawner:
    """Records launches and hands back processes the test can finish."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.launches: list[dict[str, str]] = []
        self.processes: list[FakeProcess] = []
        self.launched = asyncio.Event()

    async def __call__(self, manifest: AppManifest, env: dict[str, str]) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        self.launches.append(env)
        process = FakeProcess()
        self.processes.append(process)
        self.launched.set()
        return process


class FakePresence:
    def __init__(self, clients: list[str] | None = None) -> None:
        self.clients = list(clients or [])
        self.forgotten: list[str] = []

    def list_clients(self) -> list[Any]:
        return [type("C", (), {"client_id": c})() for c in self.clients]

    def forget(self, client_id: str) -> bool:
        self.forgotten.append(client_id)
        return True


def _manifest(**overrides: Any) -> AppManifest:
    kwargs: dict[str, Any] = {
        "name": "demo",
        "version": "1.0",
        "command": ["demo-client"],
        "env_prefix": "DEMO_",
        "presence_client_id": "demo",
        "server_url_field": "server_url",
        "api_token_field": "api_token",
    }
    kwargs.update(overrides)
    return AppManifest(**kwargs)


def _supervisor(
    spawner: FakeSpawner,
    *,
    manifest: AppManifest | None = None,
    config: dict[str, Any] | None = None,
    autostart: list[str] | None = None,
    presence: FakePresence | None = None,
    events: list[Event] | None = None,
) -> AppSupervisor:
    sink = events if events is not None else []

    async def publish(event: Event) -> None:
        sink.append(event)

    async def config_fn(_name: str) -> dict[str, Any]:
        return dict(config or {})

    async def autostart_fn() -> list[str]:
        return list(autostart or [])

    return AppSupervisor(
        publish=publish,
        presence=presence,
        config_fn=config_fn,
        autostart_fn=autostart_fn,
        server_url_fn=lambda: "http://127.0.0.1:9999",
        api_token="tok",
        manifests_fn=lambda: [manifest or _manifest()],
        spawn_fn=spawner,
    )


async def _started(**kwargs: Any) -> AppSupervisor:
    """A supervisor that has discovered its apps, as the server's would have."""
    sup = _supervisor(**kwargs)
    await sup.start()
    return sup


async def _settle() -> None:
    """Let the supervision task make progress."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_apps_are_discovered_but_not_started_by_default() -> None:
    # Opt-in autostart: a microphone-listening process must not start itself.
    spawner = FakeSpawner()
    sup = _supervisor(spawner)
    await sup.start()

    (status,) = sup.list()
    assert (status.name, status.state) == ("demo", "stopped")
    assert status.autostart is False
    assert spawner.launches == []
    await sup.stop()


@pytest.mark.asyncio
async def test_autostart_launches_at_boot() -> None:
    spawner = FakeSpawner()
    sup = _supervisor(spawner, autostart=["demo"])
    await sup.start()
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)

    status = sup.status_of("demo")
    assert status.state == "running" and status.autostart is True
    assert status.pid == spawner.processes[0].pid
    await sup.stop()


@pytest.mark.asyncio
async def test_child_environment_carries_config_and_server_details() -> None:
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner, config={"wake_word": "mjölnir"})
    await sup.start_app("demo")
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)

    env = spawner.launches[0]
    assert env["DEMO_WAKE_WORD"] == "mjölnir"
    # The server fills in where it is, so a supervised client never has to be
    # told its own server's address or token.
    assert env["DEMO_SERVER_URL"] == "http://127.0.0.1:9999"
    assert env["DEMO_API_TOKEN"] == "tok"
    assert "PATH" in env  # inherits the parent environment
    await sup.stop()


@pytest.mark.asyncio
async def test_saved_config_wins_over_the_server_default() -> None:
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner, config={"server_url": "http://elsewhere:8600"})
    await sup.start_app("demo")
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)

    assert spawner.launches[0]["DEMO_SERVER_URL"] == "http://elsewhere:8600"
    await sup.stop()


@pytest.mark.asyncio
async def test_a_clean_exit_is_restarted_because_the_user_still_wants_it() -> None:
    # The deliberate divergence from adapter watch supervision: a process that
    # quits on its own is not "finished", it is gone and should come back.
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner)
    await sup.start_app("demo")
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)

    spawner.launched.clear()
    spawner.processes[0].finish(0)  # clean exit
    await asyncio.wait_for(spawner.launched.wait(), timeout=5)

    assert len(spawner.processes) == 2
    assert sup.status_of("demo").restarts == 1
    await sup.stop()


@pytest.mark.asyncio
async def test_crash_is_restarted_and_reported() -> None:
    events: list[Event] = []
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner, events=events)
    await sup.start_app("demo")
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)

    spawner.launched.clear()
    spawner.processes[0].finish(1)
    await asyncio.wait_for(spawner.launched.wait(), timeout=5)

    types = [e.type for e in events]
    assert types.count("system.app_started") == 2
    assert "system.app_exited" in types
    exited = next(e for e in events if e.type == "system.app_exited")
    assert exited.payload == {"app": "demo", "code": 1, "restarting": True}
    await sup.stop()


@pytest.mark.asyncio
async def test_a_missing_executable_fails_without_retrying() -> None:
    # Retrying will not conjure a binary onto the machine, so this reports and
    # stops rather than looping forever.
    events: list[Event] = []
    spawner = FakeSpawner(fail_with=FileNotFoundError("'demo-client' is not installed"))
    sup = await _started(spawner=spawner, events=events)
    await sup.start_app("demo")
    await _settle()

    status = sup.status_of("demo")
    assert status.state == "failed"
    assert "not installed" in status.last_error
    assert [e.type for e in events] == ["system.app_exited"]
    await sup.stop()


@pytest.mark.asyncio
async def test_start_never_raises_even_when_discovery_explodes() -> None:
    # Server.start() has no exception handling and sits outside run()'s
    # try/finally, so a raise here would kill the process and leak every task
    # started before it.
    def explodes() -> list[AppManifest]:
        raise RuntimeError("entry points unreadable")

    sup = AppSupervisor(manifests_fn=explodes)
    await sup.start()  # must not raise
    assert sup.list() == []


@pytest.mark.asyncio
async def test_stop_terminates_and_clears_presence() -> None:
    presence = FakePresence(["demo"])
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner, presence=presence)
    await sup.start_app("demo")
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)
    assert sup.status_of("demo").present is True

    await sup.stop_app("demo")

    assert spawner.processes[0].terminated is True
    assert sup.status_of("demo").state == "stopped"
    # Windows terminate() is a hard kill, so the client never sends its
    # goodbye; without this the dashboard shows a client that is long gone.
    assert presence.forgotten == ["demo"]


@pytest.mark.asyncio
async def test_stopping_does_not_restart() -> None:
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner)
    await sup.start_app("demo")
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)

    await sup.stop_app("demo")
    await _settle()

    assert len(spawner.processes) == 1  # no resurrection after a deliberate stop


@pytest.mark.asyncio
async def test_starting_twice_is_idempotent() -> None:
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner)
    await sup.start_app("demo")
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)
    await sup.start_app("demo")
    await _settle()

    assert len(spawner.processes) == 1
    await sup.stop()


@pytest.mark.asyncio
async def test_a_crash_looping_app_eventually_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A process that never manages to stay up is misconfigured, and that does
    # not fix itself. Without this it would write two events a minute into the
    # durable log forever.
    monkeypatch.setattr(supervisor_module, "_BACKOFF_START", 0.0)
    monkeypatch.setattr(supervisor_module, "_BACKOFF_MAX", 0.0)
    events: list[Event] = []
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner, events=events)
    await sup.start_app("demo")

    # Fail every process as soon as it appears.
    for _ in range(20):
        await _settle()
        for process in spawner.processes:
            process.finish(1)
        if sup.status_of("demo").state == "failed":
            break

    status = sup.status_of("demo")
    assert status.state == "failed"
    assert "giving up" in status.last_error
    assert len(spawner.processes) <= supervisor_module._MAX_CRASH_LOOP + 1
    final = [e for e in events if e.type == "system.app_exited"][-1]
    assert final.payload["restarting"] is False


@pytest.mark.asyncio
async def test_unknown_app_is_reported() -> None:
    sup = await _started(spawner=FakeSpawner())
    with pytest.raises(UnknownAppError, match="nope"):
        await sup.start_app("nope")


@pytest.mark.asyncio
async def test_presence_reports_liveness_separately_from_the_process() -> None:
    # A running process that has not heartbeated is not yet working.
    spawner = FakeSpawner()
    sup = await _started(spawner=spawner, presence=FakePresence([]))
    await sup.start_app("demo")
    await asyncio.wait_for(spawner.launched.wait(), timeout=2)

    status = sup.status_of("demo")
    assert status.state == "running" and status.present is False
    await sup.stop()
