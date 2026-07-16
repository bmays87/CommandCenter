"""Phase 2 exit criterion, end to end: approve a permission from the API.

Boots the real composed server (real uvicorn, real mediation) with a scripted
control-capable adapter injected, then exercises the full flow over HTTP/WS:
launch -> the agent requests a permission -> it appears in the interactions
API and on the WebSocket -> answering unblocks the adapter -> a second answer
is rejected with 409. A second test restarts the server mid-pending and
verifies the orphaned interaction is cancelled (ADR-0007).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import websockets

from prodeo.adapters import (
    AdapterCapabilities,
    AdapterContext,
    AdapterMetadata,
    InteractionObservation,
    InteractionRef,
    LaunchSpec,
    ObserveOnlyAdapter,
    SessionRef,
)
from prodeo.adapters.observations import StateObservation
from prodeo.config import Settings
from prodeo.mediation import Answer, InteractionKind
from prodeo.server import Server
from prodeo.sessions.state import SessionState

pytestmark = pytest.mark.integration

TOKEN = "integration-token"
NATIVE_ID = "scripted-run-1"


class ScriptedControlAdapter(ObserveOnlyAdapter):
    """Launches one scripted session that immediately asks for permission."""

    def __init__(self) -> None:
        self.metadata = AdapterMetadata(name="scripted", version="0.0.1")
        self.capabilities = AdapterCapabilities(
            launch=True, terminate=True, respond_to_permissions=True, send_prompts=True
        )
        self.ctx: AdapterContext | None = None
        self.answers: list[Answer] = []

    async def start(self, ctx: AdapterContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        pass

    async def discover_sessions(self) -> list[Any]:
        return []

    async def watch(self, session: SessionRef) -> None:
        """Report running, then a permission request, then wait forever."""
        assert self.ctx is not None
        await self.ctx.report(
            StateObservation(native_id=session.native_id, state=SessionState.RUNNING)
        )
        await self.ctx.report(
            InteractionObservation(
                native_id=session.native_id,
                interaction_native_id="tool-use-1",
                kind=InteractionKind.PERMISSION,
                title="Allow Bash?",
                body='{"command": "rm -rf build"}',
            )
        )
        await asyncio.Event().wait()

    async def launch(self, spec: LaunchSpec) -> SessionRef:
        return SessionRef(adapter="scripted", native_id=NATIVE_ID, session_id="")

    async def respond(self, interaction: InteractionRef, answer: Answer) -> None:
        self.answers.append(answer)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        node_name="itest",
        data_dir=tmp_path / "data",
        api_host="127.0.0.1",
        api_port=0,
        api_token=TOKEN,
        adapters={},
        discovery_interval_s=0,
        dashboard_dir=tmp_path / "no-dashboard",
    )


class Stack:
    def __init__(self, server: Server, adapter: ScriptedControlAdapter) -> None:
        self.server = server
        self.adapter = adapter
        self.base = f"http://127.0.0.1:{server.api.port}"
        self.ws_base = f"ws://127.0.0.1:{server.api.port}"


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    server = Server(_settings(tmp_path))
    adapter = ScriptedControlAdapter()
    server.adapters.add(adapter)
    await server.start()
    try:
        yield Stack(server, adapter)
    finally:
        await server.stop()


async def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_permission_request_is_approved_from_the_api(stack: Stack) -> None:
    async with httpx.AsyncClient(
        base_url=stack.base, headers={"Authorization": f"Bearer {TOKEN}"}
    ) as client:
        ws_url = f"{stack.ws_base}/api/ws/events?token={TOKEN}&types=interaction.*"
        async with websockets.connect(ws_url) as ws:
            # 1. launch through the API
            resp = await client.post(
                "/api/sessions", json={"adapter": "scripted", "prompt": "do the thing"}
            )
            assert resp.status_code == 201
            session = resp.json()

            # 2. the agent blocks on a permission -> interaction.requested on WS
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert frame["type"] == "interaction.requested"
            assert frame["session_id"] == session["id"]
            interaction = frame["payload"]["interaction"]
            assert interaction["kind"] == "permission"
            assert interaction["title"] == "Allow Bash?"

            # 3. the session is waiting on a human; the inbox shows it
            async def waiting() -> bool:
                s = (await client.get(f"/api/sessions/{session['id']}")).json()
                return bool(s["state"] == "waiting_on_user")

            await wait_for(waiting)
            listing = (await client.get("/api/interactions", params={"status": "pending"})).json()
            assert listing["pending"] == 1
            assert listing["interactions"][0]["id"] == interaction["id"]

            # 4. approve it -> the adapter receives the answer, session resumes
            answered = await client.post(
                f"/api/interactions/{interaction['id']}/answer", json={"decision": "allow"}
            )
            assert answered.status_code == 200
            assert answered.json()["status"] == "answered"
            assert [a.decision for a in stack.adapter.answers] == ["allow"]

            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert frame["type"] == "interaction.answered"

            async def resumed() -> bool:
                s = (await client.get(f"/api/sessions/{session['id']}")).json()
                return bool(s["state"] == "running")

            await wait_for(resumed)

            # 5. exactly-once: a second answer is rejected
            again = await client.post(
                f"/api/interactions/{interaction['id']}/answer", json={"decision": "deny"}
            )
            assert again.status_code == 409
            assert len(stack.adapter.answers) == 1


@pytest.mark.asyncio
async def test_pending_interaction_is_cancelled_on_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    server = Server(settings)
    adapter = ScriptedControlAdapter()
    server.adapters.add(adapter)
    await server.start()
    interaction_id = ""
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{server.api.port}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            await client.post("/api/sessions", json={"adapter": "scripted"})

            async def pending() -> bool:
                body = (await client.get("/api/interactions", params={"status": "pending"})).json()
                return bool(body["pending"] == 1)

            await wait_for(pending)
            body = (await client.get("/api/interactions")).json()
            interaction_id = body["interactions"][0]["id"]
    finally:
        await server.stop()

    # restart against the same data dir: the orphan must be cancelled
    reborn = Server(settings)
    reborn.adapters.add(ScriptedControlAdapter())
    await reborn.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{reborn.api.port}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            body = (await client.get("/api/interactions")).json()
            assert body["pending"] == 0
            restored = next(i for i in body["interactions"] if i["id"] == interaction_id)
            assert restored["status"] == "cancelled"

            events = (
                await client.get("/api/events", params={"type": "interaction.cancelled"})
            ).json()
            assert any(e["payload"]["interaction_id"] == interaction_id for e in events["events"])
    finally:
        await reborn.stop()
