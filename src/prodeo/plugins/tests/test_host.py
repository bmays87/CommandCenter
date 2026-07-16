"""Plugin Host: manifest resolution, config validation, containment."""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from prodeo.adapters.interface import (
    AdapterCapabilities,
    AdapterMetadata,
    ObserveOnlyAdapter,
    SessionRef,
)
from prodeo.bus import InProcessEventBus
from prodeo.events import Event
from prodeo.events import types as ev
from prodeo.notify.interface import Notification
from prodeo.plugins import PLUGIN_API_VERSION, PluginHost, PluginManifest
from prodeo.sessions.model import SessionDescriptor


class FakeEntryPoint:
    def __init__(self, name: str, obj: Any) -> None:
        self.name = name
        self._obj = obj

    def load(self) -> Any:
        return self._obj


class MinimalAdapter(ObserveOnlyAdapter):
    def __init__(self, api_version: int = 2) -> None:
        self.metadata = AdapterMetadata(
            name="minimal", version="1.0", adapter_api_version=api_version
        )
        self.capabilities = AdapterCapabilities()

    async def start(self, ctx: object) -> None: ...

    async def stop(self) -> None: ...

    async def discover_sessions(self) -> list[SessionDescriptor]:
        return []

    async def watch(self, session: SessionRef) -> None: ...


class EchoChannel:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.sent: list[Notification] = []

    @property
    def name(self) -> str:
        return "echo"

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class EchoConfig(BaseModel):
    prefix: str = "[echo]"


class StubSummarizer:
    @property
    def name(self) -> str:
        return "stub"

    async def summarize(self, instructions: str, content: str) -> str:
        return "summary"


def _host(
    bus: InProcessEventBus, eps: list[FakeEntryPoint], **config: dict[str, dict[str, Any]]
) -> PluginHost:
    return PluginHost(bus, node="test", entry_points_fn=lambda: eps, **config)


async def _drain(sub: object) -> list[Event]:
    out: list[Event] = []
    while True:
        try:
            async with asyncio.timeout(0.05):
                async for event in sub:  # type: ignore[attr-defined]
                    out.append(event)
        except TimeoutError:
            return out


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


def _adapter_manifest(**overrides: Any) -> PluginManifest:
    kwargs: dict[str, Any] = {
        "name": "minimal",
        "kind": "adapter",
        "version": "1.0",
        "factory": MinimalAdapter,
    }
    kwargs.update(overrides)
    return PluginManifest(**kwargs)


@pytest.mark.asyncio
async def test_loads_manifest_and_manifest_factory_and_legacy_forms(
    bus: InProcessEventBus,
) -> None:
    sub = bus.subscribe("system.*", name="probe")
    manifest = _adapter_manifest()

    def factory_form() -> PluginManifest:
        return _adapter_manifest(name="via-factory")

    legacy_form = MinimalAdapter  # bare zero-arg adapter factory (Phase 1)

    host = _host(
        bus,
        [
            FakeEntryPoint("direct", manifest),
            FakeEntryPoint("factory", factory_form),
            FakeEntryPoint("legacy", legacy_form),
        ],
    )
    loaded = await host.load()

    assert len(loaded.adapters) == 3
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.SYSTEM_PLUGIN_LOADED] * 3
    assert events[0].payload == {"plugin": "minimal", "kind": "adapter", "version": "1.0"}


@pytest.mark.asyncio
async def test_notifier_plugin_gets_validated_config(bus: InProcessEventBus) -> None:
    manifest = PluginManifest(
        name="echo",
        kind="notifier",
        version="1.0",
        config_model=EchoConfig,
        factory=lambda cfg: EchoChannel(cfg.prefix),
    )
    host = _host(bus, [FakeEntryPoint("echo", manifest)], channel_config={"echo": {"prefix": ">>"}})
    loaded = await host.load()

    channel = loaded.channels["echo"]
    assert isinstance(channel, EchoChannel)
    assert channel.prefix == ">>"


@pytest.mark.asyncio
async def test_summarizer_plugin_loads(bus: InProcessEventBus) -> None:
    manifest = PluginManifest(
        name="stub", kind="summarizer", version="1.0", factory=lambda cfg: StubSummarizer()
    )
    loaded = await _host(bus, [FakeEntryPoint("stub", manifest)]).load()
    assert list(loaded.summarizers) == ["stub"]


@pytest.mark.asyncio
async def test_invalid_config_is_reported_not_fatal(bus: InProcessEventBus) -> None:
    sub = bus.subscribe("system.*", name="probe")
    bad = PluginManifest(
        name="echo",
        kind="notifier",
        version="1.0",
        config_model=EchoConfig,
        factory=lambda cfg: EchoChannel(cfg.prefix),
    )
    good = _adapter_manifest()
    host = _host(
        bus,
        [FakeEntryPoint("echo", bad), FakeEntryPoint("ok", good)],
        channel_config={"echo": {"prefix": 123.45}},  # wrong type
    )
    loaded = await host.load()

    assert loaded.channels == {}
    assert len(loaded.adapters) == 1  # the good plugin still loaded
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.SYSTEM_PLUGIN_FAILED, ev.SYSTEM_PLUGIN_LOADED]
    assert events[0].payload["plugin"] == "echo"


@pytest.mark.asyncio
async def test_plugin_api_version_mismatch_is_refused(bus: InProcessEventBus) -> None:
    sub = bus.subscribe("system.*", name="probe")
    stale = _adapter_manifest(plugin_api_version=PLUGIN_API_VERSION + 1)
    loaded = await _host(bus, [FakeEntryPoint("stale", stale)]).load()

    assert loaded.adapters == []
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.SYSTEM_PLUGIN_FAILED]
    assert "version mismatch" in events[0].payload["error"]


@pytest.mark.asyncio
async def test_adapter_api_version_mismatch_is_refused(bus: InProcessEventBus) -> None:
    stale = _adapter_manifest(factory=lambda: MinimalAdapter(api_version=1))
    loaded = await _host(bus, [FakeEntryPoint("stale", stale)]).load()
    assert loaded.adapters == []


@pytest.mark.asyncio
async def test_broken_entry_point_and_wrong_product_are_contained(
    bus: InProcessEventBus,
) -> None:
    def explodes() -> None:
        raise ImportError("missing dependency")

    wrong = PluginManifest(
        name="wrong", kind="notifier", version="1.0", factory=lambda cfg: object()
    )
    loaded = await _host(
        bus, [FakeEntryPoint("boom", explodes), FakeEntryPoint("wrong", wrong)]
    ).load()
    assert loaded.adapters == [] and loaded.channels == {} and loaded.summarizers == {}
