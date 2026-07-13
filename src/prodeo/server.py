"""Composition root - the only module where concrete implementations are wired.

Phase 0 boots the bus, the SQLite event store, and the recorder; emits
``system.started``; then idles until interrupted. The API layer, Session
Registry, and Adapter Manager attach here in Phase 1.
"""

import asyncio
import contextlib
import signal

import structlog

from prodeo import __version__
from prodeo.bus import InProcessEventBus
from prodeo.config import Settings
from prodeo.events import new_event
from prodeo.events import types as ev
from prodeo.logging import configure_logging
from prodeo.persistence import EventRecorder, SqliteEventStore

_log = structlog.get_logger(__name__)


class Server:
    """Owns the lifecycle of all core services."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = InProcessEventBus()
        self.store = SqliteEventStore(settings.event_db_path)
        self.recorder = EventRecorder(self.bus, self.store)

    async def start(self) -> None:
        await self.store.open()
        await self.recorder.start()
        await self.bus.publish(
            new_event(
                ev.SYSTEM_STARTED,
                node=self.settings.node_name,
                payload={"version": __version__},
            )
        )
        _log.info("server.started", node=self.settings.node_name, version=__version__)

    async def stop(self) -> None:
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
