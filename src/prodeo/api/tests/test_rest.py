"""REST surface: health, sessions, events, auth. (WS is covered in tests/integration.)"""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from prodeo.api import create_app
from prodeo.bus import InProcessEventBus
from prodeo.persistence import EventRecorder, SqliteEventStore
from prodeo.sessions import SessionDescriptor, SessionRegistry, SessionState

TOKEN = "secret-token"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.bus = InProcessEventBus()
        self.store = SqliteEventStore(tmp_path / "events.db")
        self.recorder = EventRecorder(self.bus, self.store)
        self.registry = SessionRegistry(self.bus)
        app = create_app(
            registry=self.registry,
            store=self.store,
            bus=self.bus,
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
    yield e
    await e.client.aclose()
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
