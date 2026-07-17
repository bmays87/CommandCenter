"""Composition root - the only module where concrete implementations are wired.

Boots: bus -> store -> recorder -> registry, mediation, and scheduler
(rebuilt from the log, in that order) -> plugin host (adapters, channels,
summarizers via entry points) -> adapter manager -> scheduler/summary/
retention loops -> API server; emits ``system.started``; then idles until
interrupted.
"""

import asyncio
import contextlib
import signal

import structlog

from prodeo import __version__
from prodeo.adapters import AdapterManager
from prodeo.api import ApiServer, create_app
from prodeo.bus import InProcessEventBus
from prodeo.config import Settings
from prodeo.events import new_event
from prodeo.events import types as ev
from prodeo.logging import configure_logging
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
                api_token=settings.api_token,
                dashboard_dir=settings.dashboard_dir,
            ),
            host=settings.api_host,
            port=settings.api_port,
        )

    async def start(self) -> None:
        await self.store.open()
        await self.recorder.start()
        await self.registry.rebuild(self.store)
        # After the recorder so orphan cancellations reach the log (ADR-0007).
        await self.mediation.rebuild(self.store)
        await self.scheduler.rebuild(self.store)
        await self.bus.publish(
            new_event(
                ev.SYSTEM_STARTED,
                node=self.settings.node_name,
                payload={"version": __version__},
            )
        )
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
        _log.info(
            "server.started",
            node=self.settings.node_name,
            version=__version__,
            api=f"http://{self.settings.api_host}:{self.api.port}",
        )

    async def stop(self) -> None:
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


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    server = Server(settings)
    await server.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # non-POSIX platforms
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await server.stop()


def main() -> None:
    """Console entry point: ``prodeo-server``."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
