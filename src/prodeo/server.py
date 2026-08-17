"""Composition root - the only module where concrete implementations are wired.

Boots: bus -> store -> recorder -> registry, mediation, and scheduler
(rebuilt from the log, in that order) -> plugin host (adapters, channels,
summarizers via entry points) -> adapter manager -> scheduler/summary/
retention loops -> API server; emits ``system.started``; then idles until
interrupted.
"""

import asyncio
import contextlib
import os
import shutil
import signal
import sys

import structlog

from prodeo import __version__
from prodeo.adapters import AdapterManager
from prodeo.api import ApiServer, create_app
from prodeo.apps import AppSupervisor
from prodeo.bus import InProcessEventBus
from prodeo.config import Settings
from prodeo.events import new_event
from prodeo.events import types as ev
from prodeo.extensions import (
    AssetProvisioner,
    BundledCatalog,
    ExtensionService,
    JsonFileConfigStore,
    TargetDirInstaller,
    activate_extension_path,
    local_index_dir,
    workspace_root,
)
from prodeo.identity import IdentityProvider
from prodeo.logging import configure_logging
from prodeo.machines import MachineRegistry
from prodeo.machines.enrollments import Enrollments
from prodeo.machines.installers import InstallerBuilder
from prodeo.machines.pairing import CcanPairingClient
from prodeo.mediation import MediationService
from prodeo.notify import Notifier
from prodeo.notify.channels import channels_from_config
from prodeo.persistence import EventRecorder, RetentionService, SqliteEventStore
from prodeo.plugins import PluginHost
from prodeo.presence import PresenceTracker
from prodeo.scheduler import SchedulerService
from prodeo.sessions import SessionRegistry
from prodeo.summary import SummaryService

_log = structlog.get_logger(__name__)


class Server:
    """Owns the lifecycle of all core services."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Set to end run()'s idle wait, by a signal handler or by the restart
        # endpoint. One event for both, so there is a single shutdown path.
        self.shutdown = asyncio.Event()
        #: Whether run() should tell main() to re-exec rather than exit.
        self.restart_wanted = False
        self.bus = InProcessEventBus()
        self.store = SqliteEventStore(settings.event_db_path)
        self.recorder = EventRecorder(self.bus, self.store)
        self.registry = SessionRegistry(self.bus, node=settings.node_name)
        self.mediation = MediationService(
            self.bus,
            node=settings.node_name,
            default_timeout_s=settings.mediation_default_timeout_s,
        )
        self.adapters = AdapterManager(
            self.bus,
            self.registry,
            self.mediation,
            data_dir=settings.data_dir,
            node=settings.node_name,
            adapter_config=settings.adapters,
            discovery_interval=settings.discovery_interval_s,
        )
        self.scheduler = SchedulerService(
            self.bus,
            self.adapters,
            node=settings.node_name,
            tz=settings.scheduler_tz,
        )
        self.machines = MachineRegistry(self.bus, node=settings.node_name)
        # The hub's identity + the CCAN onboarding path (ADR-0025). The
        # certificate is minted lazily (first get()), forced at start().
        self.identity = IdentityProvider(settings.identity_dir, common_name=settings.node_name)
        enrollments = Enrollments(settings.identity_dir / "enrollments.json")
        self.pairing = CcanPairingClient(self.identity, enrollments, hub_node=settings.node_name)
        self.installers = InstallerBuilder(
            workspace=workspace_root(),
            # Shared with the extensions manager: one wheel cache per data dir.
            wheels_dir=local_index_dir(settings.data_dir),
            out_dir=settings.installer_out_dir,
            identity=self.identity,
            enrollments=enrollments,
            hub_node=settings.node_name,
            hub_address_hint=settings.public_url,
        )
        self.retention = RetentionService(
            self.bus,
            self.store,
            self.registry,
            archive_dir=settings.archive_dir,
            rules=list(settings.retention_rules),
            archive_sessions_after_days=settings.retention_archive_sessions_after_days,
            interval_s=settings.retention_interval_s,
            node=settings.node_name,
        )
        self.presence = PresenceTracker()
        self.notifier = Notifier(
            self.bus,
            channels_from_config(settings.notify_channels),
            settings.notify_rules,
            node=settings.node_name,
            public_url=settings.public_url,
            attention=self.presence,
            away_only_channels=settings.notify_away_only_channels,
        )
        self.plugins = PluginHost(
            self.bus,
            node=settings.node_name,
            adapter_config=settings.adapters,
            channel_config=settings.notify_channels,
            plugin_config=settings.plugins,
        )
        self.summary = SummaryService(
            self.bus,
            self.store,
            self.registry,
            cron=settings.summary_cron,
            tz=settings.scheduler_tz,
            node=settings.node_name,
        )
        # Runtime-installed extensions live outside .venv (so `uv sync` cannot
        # delete them) and become importable by joining sys.path here, before
        # the Plugin Host discovers entry points in start().
        self.extension_lib = activate_extension_path(settings.data_dir)
        # The extensions manager reads the host's inventory lazily: it is wired
        # here, but the host does not populate it until start() (ADR-0014).
        self.extension_config = JsonFileConfigStore(settings.extension_config_path)
        self.extensions = ExtensionService(
            inventory_fn=self.plugins.inventory,
            env_config={
                "adapter": settings.adapters,
                "notifier": settings.notify_channels,
                "summarizer": settings.plugins,
            },
            store=self.extension_config,
            catalog=BundledCatalog(),
            installer=TargetDirInstaller(
                self.extension_lib,
                # In a checkout, first-party extensions are unpublished and
                # depend on each other, so they can only be installed from
                # locally built wheels. Elsewhere both are None and installs
                # come from the package index as normal.
                find_links=local_index_dir(settings.data_dir),
                workspace=workspace_root(),
            ),
            publish=self.bus.publish,
            node=settings.node_name,
            default_models_dir=settings.default_models_dir,
        )
        self.assets = AssetProvisioner(
            catalog=BundledCatalog(),
            store=self.extension_config,
            models_dir_fn=self._models_dir,
        )
        # Supervises app-class extensions (Mjölnir). Started last and stopped
        # first: the children are HTTP clients of this server, so the listener
        # must exist before them and outlive them. server_url is a callable
        # because api_port may be 0 and the real port is only known once the
        # API is up.
        self.apps = AppSupervisor(
            node=settings.node_name,
            publish=self.bus.publish,
            presence=self.presence,
            config_fn=self._app_config,
            autostart_fn=self._app_autostart,
            server_url_fn=lambda: f"http://{settings.api_host}:{self.api.port}",
            api_token=settings.api_token,
            # The readiness probe (ADR-0017): what the catalog says this app
            # still needs before starting it would do anything but crash-loop.
            setup_gaps_fn=self.assets.unmet_for_app,
        )
        self.api = ApiServer(
            create_app(
                registry=self.registry,
                store=self.store,
                bus=self.bus,
                mediation=self.mediation,
                manager=self.adapters,
                scheduler=self.scheduler,
                presence=self.presence,
                node=settings.node_name,
                version=__version__,
                extensions=self.extensions,
                catalog=BundledCatalog(),
                apps=self.apps,
                assets=self.assets,
                api_token=settings.api_token,
                dashboard_dir=settings.dashboard_dir,
                restart_fn=self.request_restart,
                machines=self.machines,
                pairing=self.pairing,
                installers=self.installers,
            ),
            host=settings.api_host,
            port=settings.api_port,
        )

    def request_restart(self) -> None:
        """Ask the process to stop and come back.

        Only records the intent and releases run()'s wait: the re-exec happens
        in main(), once the event loop has fully unwound. Doing it here would
        replace the process image with children still running and the event
        store still open.
        """
        _log.info("server.restart_requested")
        self.restart_wanted = True
        self.shutdown.set()

    async def _models_dir(self) -> str:
        """The chosen models root, or the default under the data dir."""
        return (await self.extensions.settings()).models_dir

    async def _app_config(self, name: str) -> dict[str, object]:
        """Saved config for an app, from the same store plugins use."""
        return dict(await self.extension_config.get(name) or {})

    async def _app_autostart(self) -> list[str]:
        return list((await self.extension_config.settings()).autostart)

    async def start(self) -> None:
        await self.store.open()
        await self.recorder.start()
        await self.registry.rebuild(self.store)
        # After the recorder so orphan cancellations reach the log (ADR-0007).
        await self.mediation.rebuild(self.store)
        await self.scheduler.rebuild(self.store)
        await self.machines.rebuild(self.store)
        # After the rebuild, so the hub's machine is added exactly once ever.
        await self.machines.ensure_local()
        # Mint (or load) the hub certificate now rather than on first use, so
        # every boot logs the same fingerprint installers will carry.
        await self.identity.get()
        await self.bus.publish(
            new_event(
                ev.SYSTEM_STARTED,
                node=self.settings.node_name,
                payload={"version": __version__},
            )
        )
        # The saved overlay and disabled set must land before load(): the host
        # reads both at instantiation time, so applying them later is a no-op.
        state = await self.extension_config.state()
        self.plugins.apply_saved_config(state.config)
        self.plugins.apply_disabled(state.disabled)
        loaded = await self.plugins.load()
        for adapter in loaded.adapters:
            self.adapters.add(adapter)
        for name, channel in loaded.channels.items():
            self.notifier.add_channel(name, channel)
        self.summary.set_summarizer(
            loaded.summarizers.get(self.settings.summary_plugin)
            or next(iter(loaded.summarizers.values()), None)
        )
        await self.notifier.start()
        await self.adapters.start()
        # After the adapters, so a due schedule finds its adapter loaded.
        await self.scheduler.start()
        await self.summary.start()
        await self.retention.start()
        await self.api.start()
        # Last: supervised apps are HTTP clients of this server and need the
        # resolved port, which only exists once the API is listening.
        await self.apps.start()
        _log.info(
            "server.started",
            node=self.settings.node_name,
            version=__version__,
            api=f"http://{self.settings.api_host}:{self.api.port}",
        )

    async def stop(self) -> None:
        # First: kill the children before the server they talk to disappears.
        await self.apps.stop()
        await self.api.stop()
        await self.retention.stop()
        await self.summary.stop()
        await self.scheduler.stop()
        await self.adapters.stop()
        await self.notifier.stop()
        await self.mediation.close()
        await self.bus.publish(new_event(ev.SYSTEM_STOPPING, node=self.settings.node_name))
        await self.recorder.stop()
        await self.bus.close()
        await self.store.close()
        _log.info("server.stopped")


async def run(settings: Settings | None = None) -> bool:
    """Boot, idle until stopped, tear down. True = the caller should re-exec."""
    settings = settings or Settings()
    configure_logging(settings.log_level)
    server = Server(settings)
    await server.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # non-POSIX platforms
            loop.add_signal_handler(sig, server.shutdown.set)
    try:
        await server.shutdown.wait()
    finally:
        await server.stop()
    return server.restart_wanted


def reexec_argv() -> list[str]:
    """The argv that starts this server again, however it was started.

    Three launch shapes have to survive a restart: ``python -m prodeo.server``,
    the ``prodeo-server`` console script (a ``.exe`` on Windows, a shebang
    script on POSIX - both directly executable), and a plain script path.
    ``sys.argv[0]`` alone is not enough: under ``-m`` it is the resolved module
    file, which is not runnable on its own.

    The ``-m`` test is ``__main__.__spec__``, but only when it names a real
    module. A console script launched through ``runpy`` - which is what
    ``uv run prodeo-server`` does - also has a ``__spec__``, named plain
    ``"__main__"`` with an empty ``parent``. Treating that as ``-m`` yields
    ``python -m ""``, which is how this was first shipped and how it failed.
    """
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    name: str = getattr(spec, "name", "") or ""
    if name and name != "__main__":
        # "prodeo.__main__" for a package, "prodeo.server" for a module.
        module = getattr(spec, "parent", "") if name.endswith(".__main__") else name
        if module:
            return [sys.executable, "-m", module, *sys.argv[1:]]
    argv0 = sys.argv[0]
    if argv0.endswith(".py"):
        return [sys.executable, argv0, *sys.argv[1:]]
    return [shutil.which(argv0) or argv0, *sys.argv[1:]]


def main() -> None:
    """Console entry point: ``prodeo-server``."""
    with contextlib.suppress(KeyboardInterrupt):
        if asyncio.run(run()):
            # After asyncio.run: the loop is unwound, children reaped by
            # Server.stop(), and the event store closed, so the replacement
            # process finds the port free and the database consistent.
            argv = reexec_argv()
            _log.info("server.restarting", command=argv)
            try:
                os.execv(argv[0], argv)
            except OSError:
                # The old process is already gone at this point, so a failure
                # here means no server at all. Say exactly what was attempted:
                # the user's next move is to run it by hand.
                _log.exception("server.restart_failed", command=argv)
                raise SystemExit(1) from None
