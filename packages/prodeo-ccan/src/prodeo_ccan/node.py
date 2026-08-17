"""The CCAN's local stack: the machine-bound half of the split (ADR-0020).

Exactly the pieces the colocated server runs, minus everything hub-shaped:
a local bus, its own event log (the hub mirrors it — ADR-0026), the session
registry and mediation service for *this machine's* sessions, and the
adapter manager with entry-point-discovered adapters. No notifier, no
scheduler, no dashboard: those stay on the hub, which sees everything that
happens here through the mirrored log.
"""

import structlog

from prodeo.adapters import AdapterManager
from prodeo.bus import InProcessEventBus
from prodeo.mediation import MediationService
from prodeo.persistence import EventRecorder, SqliteEventStore
from prodeo.plugins import PluginHost
from prodeo.sessions import SessionRegistry
from prodeo_ccan.config import CcanConfig

_log = structlog.get_logger(__name__)


class NodeHost:
    """Owns the lifecycle of the node's services (mirrors ``prodeo.server``)."""

    def __init__(self, config: CcanConfig, *, plugins: PluginHost | None = None) -> None:
        self.config = config
        self.bus = InProcessEventBus()
        self.store = SqliteEventStore(config.data_dir / "events.db")
        self.recorder = EventRecorder(self.bus, self.store)
        self.registry = SessionRegistry(self.bus, node=config.node_name)
        self.mediation = MediationService(self.bus, node=config.node_name)
        adapter_config = {k: dict(v) for k, v in config.adapters.items()}
        self.adapters = AdapterManager(
            self.bus,
            self.registry,
            self.mediation,
            data_dir=config.data_dir,
            node=config.node_name,
            adapter_config=adapter_config,
            discovery_interval=config.discovery_interval_s,
        )
        self.plugins = plugins or PluginHost(
            self.bus, node=config.node_name, adapter_config=adapter_config
        )

    async def start(self) -> None:
        await self.store.open()
        await self.recorder.start()
        await self.registry.rebuild(self.store)
        await self.mediation.rebuild(self.store)
        loaded = await self.plugins.load()
        for adapter in loaded.adapters:
            self.adapters.add(adapter)
        await self.adapters.start()
        _log.info(
            "ccan.started",
            node=self.config.node_name,
            adapters=[a.name for a in self.adapters.describe_adapters()],
        )

    async def stop(self) -> None:
        await self.adapters.stop()
        await self.mediation.close()
        await self.recorder.stop()
        await self.bus.close()
        await self.store.close()
