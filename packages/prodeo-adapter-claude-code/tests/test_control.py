"""SDK control path: launch, permission bridging, respond, terminate.

The SDK is never touched: a fake client scripted per-test stands in via the
injectable ``client_factory``.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from prodeo.adapters.interface import InteractionRef, LaunchSpec, SessionRef
from prodeo.adapters.observations import (
    InteractionObservation,
    Observation,
    StateObservation,
)
from prodeo.adapters.testing import recording_context
from prodeo.errors import CapabilityNotSupportedError
from prodeo.mediation.model import Answer
from prodeo_adapter_claude_code.adapter import ClaudeCodeAdapter
from prodeo_adapter_claude_code.launcher import DecideFn

if TYPE_CHECKING:
    from prodeo.adapters.context import AdapterContext

SESSION_ID = "0a1b2c3d-e4f5-6789-abcd-ef0123456789"


@dataclass
class InitMessage:
    session_id: str = SESSION_ID


class FakeSdkClient:
    """Scriptable stand-in for ClaudeSDKClient."""

    def __init__(self, spec: LaunchSpec, decide: DecideFn) -> None:
        self.spec = spec
        self.decide = decide
        self.connected = False
        self.queries: list[str] = []
        self.interrupted = False
        self.disconnected = False
        self._messages: asyncio.Queue[Any] = asyncio.Queue()
        self._messages.put_nowait(InitMessage())

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_messages(self) -> AsyncIterator[Any]:
        while True:
            item = await self._messages.get()
            if isinstance(item, Exception):
                raise item
            yield item

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True

    def fail_stream(self, exc: Exception) -> None:
        """Make the message stream raise (simulates the CLI process dying)."""
        self._messages.put_nowait(exc)

    async def request_permission(self, tool: str, input_data: dict[str, Any]) -> Answer:
        """Simulate the SDK invoking can_use_tool (via the decide bridge)."""
        return await self.decide(tool, input_data)


class Harness:
    """One started adapter with a fake-SDK factory and recorded observations."""

    def __init__(self, tmp_path: Path, config: dict[str, Any] | None = None) -> None:
        self.clients: list[FakeSdkClient] = []

        def factory(spec: LaunchSpec, decide: DecideFn) -> FakeSdkClient:
            client = FakeSdkClient(spec, decide)
            self.clients.append(client)
            return client

        self.adapter = ClaudeCodeAdapter(client_factory=factory)
        self.projects = tmp_path / "projects"
        self.ctx: AdapterContext
        self.observations: list[Observation]
        self.ctx, self.observations = recording_context(
            "claude-code",
            tmp_path / "data",
            {"projects_dir": str(self.projects), **(config or {})},
        )

    async def start(self) -> None:
        await self.adapter.start(self.ctx)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


@pytest.mark.asyncio
async def test_control_capabilities_declared_with_factory(harness: Harness) -> None:
    caps = harness.adapter.capabilities
    assert caps.launch and caps.terminate and caps.respond_to_permissions and caps.send_prompts
    assert not caps.answer_questions


@pytest.mark.asyncio
async def test_control_disabled_by_config(tmp_path: Path) -> None:
    harness = Harness(tmp_path, {"control_enabled": False})
    await harness.start()
    assert not harness.adapter.capabilities.launch
    with pytest.raises(CapabilityNotSupportedError):
        await harness.adapter.launch(LaunchSpec())
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_launch_returns_native_id_from_init_message(harness: Harness) -> None:
    await harness.start()
    ref = await harness.adapter.launch(LaunchSpec(project="/p", prompt="fix it"))

    assert ref.native_id == SESSION_ID
    client = harness.clients[0]
    assert client.connected
    assert client.queries == ["fix it"]
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_permission_bridges_to_interaction_and_respond_resolves(harness: Harness) -> None:
    await harness.start()
    await harness.adapter.launch(LaunchSpec(prompt="go"))
    client = harness.clients[0]

    ask = asyncio.create_task(client.request_permission("Bash", {"command": "rm -rf build"}))
    await asyncio.sleep(0.05)  # let the callback park on its future

    interactions = [o for o in harness.observations if isinstance(o, InteractionObservation)]
    assert len(interactions) == 1
    obs = interactions[0]
    assert obs.native_id == SESSION_ID
    assert obs.kind == "permission"
    assert "rm -rf build" in obs.body
    assert not ask.done()  # blocked until a human answers

    await harness.adapter.respond(
        InteractionRef(
            adapter="claude-code",
            session_native_id=SESSION_ID,
            interaction_id="cc-interaction",
            native_id=obs.interaction_native_id,
        ),
        Answer(decision="allow"),
    )
    answer = await asyncio.wait_for(ask, timeout=1)
    assert answer.decision == "allow"
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_respond_to_unknown_interaction_raises(harness: Harness) -> None:
    await harness.start()
    await harness.adapter.launch(LaunchSpec(prompt="go"))
    with pytest.raises(RuntimeError):
        await harness.adapter.respond(
            InteractionRef(
                adapter="claude-code",
                session_native_id=SESSION_ID,
                interaction_id="x",
                native_id="ghost",
            ),
            Answer(decision="allow"),
        )
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_terminate_interrupts_and_reports_stopped(harness: Harness) -> None:
    await harness.start()
    ref = await harness.adapter.launch(LaunchSpec(prompt="go"))

    await harness.adapter.terminate(ref)

    client = harness.clients[0]
    assert client.interrupted and client.disconnected
    stopped = [
        o
        for o in harness.observations
        if isinstance(o, StateObservation) and o.reason == "terminated"
    ]
    assert len(stopped) == 1 and stopped[0].native_id == SESSION_ID
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_control_refused_for_sessions_we_did_not_launch(harness: Harness) -> None:
    await harness.start()
    foreign = SessionRef(adapter="claude-code", native_id="manual-session", session_id="s")
    with pytest.raises(RuntimeError):
        await harness.adapter.terminate(foreign)
    with pytest.raises(RuntimeError):
        await harness.adapter.send_prompt(foreign, "hi")
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_send_prompt_queues_on_owned_session(harness: Harness) -> None:
    await harness.start()
    ref = await harness.adapter.launch(LaunchSpec(prompt="go"))
    await harness.adapter.send_prompt(ref, "and then?")
    assert harness.clients[0].queries == ["go", "and then?"]
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_watch_waits_for_transcript_of_owned_session(tmp_path: Path) -> None:
    harness = Harness(tmp_path, {"poll_interval_s": 0.02, "idle_timeout_s": 60})
    await harness.start()
    ref = await harness.adapter.launch(LaunchSpec(prompt="go"))

    watch = asyncio.create_task(
        harness.adapter.watch(
            SessionRef(adapter="claude-code", native_id=ref.native_id, session_id="cc")
        )
    )
    await asyncio.sleep(0.1)  # transcript not there yet: watch must be waiting
    assert not watch.done()
    assert not any(isinstance(o, StateObservation) for o in harness.observations)

    transcript = harness.projects / "proj" / f"{ref.native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type": "user", "message": {"role": "user", "content": "hello"}}\n', encoding="utf-8"
    )
    deadline = asyncio.get_running_loop().time() + 2
    while not harness.observations and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert harness.observations, "watch never picked up the late transcript"

    watch.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watch
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_sdk_stream_failure_reports_failed_state(harness: Harness) -> None:
    await harness.start()
    ref = await harness.adapter.launch(LaunchSpec(prompt="go"))

    harness.clients[0].fail_stream(RuntimeError("CLI process died"))
    deadline = asyncio.get_running_loop().time() + 1
    failed: list[StateObservation] = []
    while not failed and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
        failed = [
            o
            for o in harness.observations
            if isinstance(o, StateObservation) and o.state == "failed"
        ]
    assert failed and failed[0].native_id == ref.native_id
    assert "CLI process died" in failed[0].reason
    await harness.adapter.stop()


@pytest.mark.asyncio
async def test_default_client_factory_marks_sessions_managed() -> None:
    """The PRODEO_MANAGED marker keeps the PermissionRequest hook out of
    SDK-launched sessions (can_use_tool already mediates them, ADR-0011)."""
    from typing import cast

    from prodeo_adapter_claude_code.launcher import default_client_factory

    async def decide(tool_name: str, input_data: dict[str, Any]) -> Answer:
        return Answer(decision="allow")

    client = default_client_factory(LaunchSpec(options={"env": {"KEEP": "me"}}), decide)

    env = cast("Any", client).options.env
    assert env["PRODEO_MANAGED"] == "1"
    assert env["KEEP"] == "me"  # caller-supplied env survives the merge


@pytest.mark.asyncio
async def test_launch_without_init_message_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import prodeo_adapter_claude_code.launcher as launcher_mod

    class SilentClient(FakeSdkClient):
        def __init__(self, spec: LaunchSpec, decide: DecideFn) -> None:
            super().__init__(spec, decide)
            self._messages = asyncio.Queue()  # no init message ever

    harness = Harness(tmp_path)
    harness.adapter = ClaudeCodeAdapter(client_factory=lambda s, d: SilentClient(s, d))
    await harness.start()
    monkeypatch.setattr(launcher_mod, "INIT_TIMEOUT_S", 0.05)
    with pytest.raises(TimeoutError):
        await harness.adapter.launch(LaunchSpec(prompt="go"))
    await harness.adapter.stop()
