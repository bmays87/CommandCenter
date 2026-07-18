"""REST surface: health, sessions, events, commands, auth.

(WS is covered in tests/integration.)
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from prodeo.adapters import (
    AdapterCapabilities,
    AdapterContext,
    AdapterManager,
    AdapterMetadata,
    InteractionRef,
    LaunchSpec,
    ObserveOnlyAdapter,
    SessionRef,
)
from prodeo.api import create_app
from prodeo.bus import InProcessEventBus
from prodeo.mediation import (
    Answer,
    Interaction,
    InteractionKind,
    InteractionRequest,
    InteractionStatus,
    MediationService,
)
from prodeo.persistence import EventRecorder, SqliteEventStore
from prodeo.presence import PresenceTracker
from prodeo.scheduler import SchedulerService
from prodeo.sessions import SessionDescriptor, SessionRegistry, SessionState

TOKEN = "secret-token"


class FakeControlAdapter(ObserveOnlyAdapter):
    """Minimal control-capable adapter for exercising the command routes."""

    def __init__(self) -> None:
        self.metadata = AdapterMetadata(name="fake", version="0.0.1")
        self.capabilities = AdapterCapabilities(
            launch=True, terminate=True, respond_to_permissions=True, send_prompts=True
        )
        self.responses: list[tuple[InteractionRef, Answer]] = []
        self.terminated: list[str] = []
        self.prompts: list[str] = []

    async def start(self, ctx: AdapterContext) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def discover_sessions(self) -> list[SessionDescriptor]:
        return []

    async def watch(self, session: SessionRef) -> None:
        pass

    async def launch(self, spec: LaunchSpec) -> SessionRef:
        return SessionRef(adapter="fake", native_id="launched-1", session_id="")

    async def terminate(self, session: SessionRef) -> None:
        self.terminated.append(session.native_id)

    async def respond(self, interaction: InteractionRef, answer: Answer) -> None:
        self.responses.append((interaction, answer))

    async def send_prompt(self, session: SessionRef, prompt: str) -> None:
        self.prompts.append(prompt)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.bus = InProcessEventBus()
        self.store = SqliteEventStore(tmp_path / "events.db")
        self.recorder = EventRecorder(self.bus, self.store)
        self.registry = SessionRegistry(self.bus)
        self.mediation = MediationService(self.bus)
        self.adapter = FakeControlAdapter()
        self.manager = AdapterManager(
            self.bus, self.registry, self.mediation, data_dir=tmp_path, discovery_interval=0
        )
        self.manager.add(self.adapter)
        self.scheduler = SchedulerService(self.bus, self.manager, node="test-node")
        self.presence = PresenceTracker()
        app = create_app(
            registry=self.registry,
            store=self.store,
            bus=self.bus,
            mediation=self.mediation,
            manager=self.manager,
            scheduler=self.scheduler,
            presence=self.presence,
            node="test-node",
            version="0.0-test",
            api_token=TOKEN,
        )
        transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(
            transport=transport,
            base_url="http://api",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = Env(tmp_path)
    await e.store.open()
    await e.recorder.start()
    await e.manager.start()
    yield e
    await e.client.aclose()
    await e.manager.stop()
    await e.recorder.stop()
    await e.bus.close()
    await e.store.close()


@pytest.mark.asyncio
async def test_health_is_open_and_reports_identity(env: Env) -> None:
    resp = await env.client.get("/api/health", headers={"Authorization": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["node"] == "test-node"


@pytest.mark.asyncio
async def test_missing_or_wrong_token_is_rejected(env: Env) -> None:
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}):
        resp = await env.client.get("/api/sessions", headers=headers)
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_via_query_param_is_accepted(env: Env) -> None:
    resp = await env.client.get(
        "/api/sessions", params={"token": TOKEN}, headers={"Authorization": ""}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_sessions_listing_and_detail(env: Env) -> None:
    session = await env.registry.upsert_discovered(
        "claude-code", SessionDescriptor(native_id="n1", title="T", project="/p")
    )

    listing = (await env.client.get("/api/sessions")).json()
    assert [s["id"] for s in listing["sessions"]] == [session.id]
    assert listing["sessions"][0]["state"] == "running"

    detail = (await env.client.get(f"/api/sessions/{session.id}")).json()
    assert detail["title"] == "T"

    assert (await env.client.get("/api/sessions/nope")).status_code == 404


@pytest.mark.asyncio
async def test_events_query_with_cursor_and_filters(env: Env) -> None:
    session = await env.registry.upsert_discovered("claude-code", SessionDescriptor(native_id="n1"))
    await env.registry.observe_state(session.id, SessionState.COMPLETED, reason="done")
    await env.recorder.stop()  # flush: recorder queue drained before stop returns

    body = (await env.client.get("/api/events")).json()
    types = [e["type"] for e in body["events"]]
    assert "session.discovered" in types and "session.completed" in types
    assert body["cursor"] == body["events"][-1]["id"]

    # cursor pages strictly forward
    first_id = body["events"][0]["id"]
    after = (await env.client.get("/api/events", params={"after": first_id})).json()
    assert all(e["id"] > first_id for e in after["events"])

    # type pattern filter
    only_state = await env.client.get("/api/events", params={"type": "session.state_changed"})
    assert {e["type"] for e in only_state.json()["events"]} == {"session.state_changed"}

    # per-session route
    per_session = (await env.client.get(f"/api/sessions/{session.id}/events")).json()
    assert all(e["session_id"] == session.id for e in per_session["events"])
    assert (await env.client.get("/api/sessions/nope/events")).status_code == 404


@pytest.mark.asyncio
async def test_openapi_schema_is_served(env: Env) -> None:
    resp = await env.client.get("/openapi.json", headers={"Authorization": ""})
    assert resp.status_code == 200
    assert "/api/sessions" in resp.json()["paths"]


async def _open_interaction(env: Env) -> str:
    """Open one pending permission interaction; returns its id."""
    session = await env.registry.upsert_discovered("fake", SessionDescriptor(native_id="n1"))

    async def deliver(interaction: Interaction, answer: Answer) -> None:
        ref = InteractionRef(
            adapter="fake",
            session_native_id="n1",
            interaction_id=interaction.id,
            native_id="tool-1",
        )
        await env.adapter.respond(ref, answer)

    interaction = await env.mediation.open(
        InteractionRequest(
            session_id=session.id,
            adapter="fake",
            native_id="tool-1",
            kind=InteractionKind.PERMISSION,
            title="Run rm?",
        ),
        deliver,
    )
    return interaction.id


@pytest.mark.asyncio
async def test_interactions_listing_and_filters(env: Env) -> None:
    interaction_id = await _open_interaction(env)

    body = (await env.client.get("/api/interactions")).json()
    assert body["pending"] == 1
    assert [i["id"] for i in body["interactions"]] == [interaction_id]
    assert body["interactions"][0]["kind"] == "permission"

    pending = (await env.client.get("/api/interactions", params={"status": "pending"})).json()
    assert len(pending["interactions"]) == 1
    none = (await env.client.get("/api/interactions", params={"status": "answered"})).json()
    assert none["interactions"] == []


@pytest.mark.asyncio
async def test_answer_interaction_first_wins_then_409(env: Env) -> None:
    interaction_id = await _open_interaction(env)

    resp = await env.client.post(
        f"/api/interactions/{interaction_id}/answer", json={"decision": "allow"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "answered"
    assert [a.decision for _r, a in env.adapter.responses] == ["allow"]

    again = await env.client.post(
        f"/api/interactions/{interaction_id}/answer", json={"decision": "deny"}
    )
    assert again.status_code == 409
    assert len(env.adapter.responses) == 1

    missing = await env.client.post("/api/interactions/nope/answer", json={"decision": "allow"})
    assert missing.status_code == 404


def _external_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "adapter": "fake",
        "session_native_id": "n1",
        "title": "Allow Bash?",
        "body": '{\n  "command": "rm -rf build"\n}',
        "timeout_s": 30,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_external_interaction_long_polls_until_answered(env: Env) -> None:
    await env.registry.upsert_discovered("fake", SessionDescriptor(native_id="n1"))

    poll = asyncio.create_task(
        env.client.post("/api/interactions/external", json=_external_payload())
    )
    await _wait_for_pending(env)
    (pending,) = env.mediation.list_interactions(status=InteractionStatus.PENDING)
    assert pending.title == "Allow Bash?"

    answered = await env.client.post(
        f"/api/interactions/{pending.id}/answer",
        json={"decision": "allow", "updated_input": {"command": "rm -rf build/"}},
    )
    assert answered.status_code == 200

    resp = await asyncio.wait_for(poll, timeout=5)
    assert resp.status_code == 200
    body = resp.json()
    assert body["interaction_id"] == pending.id
    assert body["status"] == "answered"
    assert body["answer"]["decision"] == "allow"
    assert body["answer"]["updated_input"] == {"command": "rm -rf build/"}
    assert env.adapter.responses == []  # the long-poll response carries the answer


@pytest.mark.asyncio
async def test_external_interaction_timeout_returns_no_answer(env: Env) -> None:
    await env.registry.upsert_discovered("fake", SessionDescriptor(native_id="n1"))

    resp = await env.client.post(
        "/api/interactions/external", json=_external_payload(timeout_s=0.1)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "timed_out"
    assert body["answer"] is None


@pytest.mark.asyncio
async def test_external_interaction_rejects_bad_requests(env: Env) -> None:
    payload = _external_payload(session_native_id="ghost")
    assert (await env.client.post("/api/interactions/external", json=payload)).status_code == 404

    payload = _external_payload(adapter="unknown")
    assert (await env.client.post("/api/interactions/external", json=payload)).status_code == 400

    payload = _external_payload(timeout_s=0)
    assert (await env.client.post("/api/interactions/external", json=payload)).status_code == 422

    resp = await env.client.post(
        "/api/interactions/external", json=_external_payload(), headers={"Authorization": ""}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_external_interaction_client_cancel_withdraws(env: Env) -> None:
    await env.registry.upsert_discovered("fake", SessionDescriptor(native_id="n1"))

    poll = asyncio.create_task(
        env.client.post("/api/interactions/external", json=_external_payload())
    )
    await _wait_for_pending(env)
    (pending,) = env.mediation.list_interactions(status=InteractionStatus.PENDING)

    poll.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poll

    current = env.mediation.get(pending.id)
    assert current is not None and current.status == InteractionStatus.CANCELLED


async def _wait_for_pending(env: Env) -> None:
    async with asyncio.timeout(5):
        while not env.mediation.list_interactions(status=InteractionStatus.PENDING):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_launch_terminate_prompt_roundtrip(env: Env) -> None:
    resp = await env.client.post(
        "/api/sessions", json={"adapter": "fake", "project": "/p", "prompt": "fix the bug"}
    )
    assert resp.status_code == 201
    session = resp.json()
    assert session["native_id"] == "launched-1"
    assert session["state"] == "starting"

    prompted = await env.client.post(
        f"/api/sessions/{session['id']}/prompt", json={"prompt": "also add tests"}
    )
    assert prompted.status_code == 200
    assert env.adapter.prompts == ["also add tests"]

    terminated = await env.client.post(f"/api/sessions/{session['id']}/terminate")
    assert terminated.status_code == 200
    assert env.adapter.terminated == ["launched-1"]

    assert (await env.client.post("/api/sessions/nope/terminate")).status_code == 404


@pytest.mark.asyncio
async def test_launch_unknown_adapter_is_400(env: Env) -> None:
    resp = await env.client.post("/api/sessions", json={"adapter": "ghost"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_schedule_crud_roundtrip(env: Env) -> None:
    created = await env.client.post(
        "/api/schedules",
        json={"name": "nightly", "cron": "0 2 * * *", "adapter": "fake", "prompt": "tidy up"},
    )
    assert created.status_code == 201
    schedule = created.json()
    assert schedule["cron"] == "0 2 * * *"
    assert schedule["spec"]["prompt"] == "tidy up"
    assert schedule["next_run_at"] is not None

    listing = (await env.client.get("/api/schedules")).json()
    assert [s["id"] for s in listing["schedules"]] == [schedule["id"]]

    deleted = await env.client.delete(f"/api/schedules/{schedule['id']}")
    assert deleted.status_code == 204
    assert (await env.client.get("/api/schedules")).json()["schedules"] == []
    assert (await env.client.delete(f"/api/schedules/{schedule['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_schedule_invalid_cron_is_400(env: Env) -> None:
    resp = await env.client.post(
        "/api/schedules", json={"name": "x", "cron": "whenever", "adapter": "fake"}
    )
    assert resp.status_code == 400
    assert "cron" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_commands_require_auth(env: Env) -> None:
    headers = {"Authorization": ""}
    assert (
        await env.client.post("/api/sessions", json={"adapter": "fake"}, headers=headers)
    ).status_code == 401
    assert (
        await env.client.post(
            "/api/interactions/x/answer", json={"decision": "allow"}, headers=headers
        )
    ).status_code == 401
    assert (await env.client.get("/api/interactions", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_presence_report_list_and_forget(env: Env) -> None:
    reported = await env.client.put(
        "/api/presence/mjolnir",
        json={"kind": "voice", "attentive": True, "node": "kitchen-pi", "ttl_s": 30},
    )
    assert reported.status_code == 200
    assert reported.json()["attentive"] is True

    listing = (await env.client.get("/api/presence")).json()
    assert [c["client_id"] for c in listing["clients"]] == ["mjolnir"]
    assert listing["clients"][0]["kind"] == "voice"
    assert listing["any_attentive"] is True

    assert (await env.client.delete("/api/presence/mjolnir")).status_code == 204
    listing = (await env.client.get("/api/presence")).json()
    assert listing["clients"] == []
    assert listing["any_attentive"] is False
    # forgetting an unknown client is fine (expiry races are expected)
    assert (await env.client.delete("/api/presence/mjolnir")).status_code == 204


@pytest.mark.asyncio
async def test_voice_event_ingest_lands_in_the_log(env: Env) -> None:
    resp = await env.client.post(
        "/api/voice/events",
        json={
            "type": "voice.transcription_completed",
            "client_id": "mjolnir",
            "node": "kitchen-pi",
            "payload": {"text": "what happened overnight"},
        },
    )
    assert resp.status_code == 201
    event = resp.json()
    assert event["source"] == "voice:mjolnir"
    assert event["node"] == "kitchen-pi"

    await env.recorder.stop()  # flush: recorder queue drained before stop returns
    stored = (await env.client.get("/api/events", params={"type": "voice.*"})).json()
    assert [e["id"] for e in stored["events"]] == [event["id"]]
    assert stored["events"][0]["payload"]["text"] == "what happened overnight"


@pytest.mark.asyncio
async def test_voice_event_ingest_rejects_non_voice_types(env: Env) -> None:
    for bad in ("session.completed", "voice.", "voicecommand"):
        resp = await env.client.post(
            "/api/voice/events", json={"type": bad, "client_id": "mjolnir"}
        )
        assert resp.status_code == 400, bad


@pytest.mark.asyncio
async def test_presence_and_voice_ingest_require_auth(env: Env) -> None:
    headers = {"Authorization": ""}
    assert (await env.client.get("/api/presence", headers=headers)).status_code == 401
    assert (
        await env.client.put("/api/presence/x", json={"kind": "voice"}, headers=headers)
    ).status_code == 401
    assert (
        await env.client.post(
            "/api/voice/events",
            json={"type": "voice.speech_started", "client_id": "x"},
            headers=headers,
        )
    ).status_code == 401
