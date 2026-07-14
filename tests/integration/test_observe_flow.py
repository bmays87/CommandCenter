"""Phase 1 exit criterion, end to end: a Claude Code session appears live.

Boots the real composed server (real uvicorn on an ephemeral port, real
claude-code adapter loaded via its entry point) against a fake Claude Code
projects directory, then checks the REST surface and the live WebSocket
stream, including auth.
"""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import websockets

from prodeo.config import Settings
from prodeo.server import Server

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parents[1] / "fixtures" / "claude-code-session.jsonl"
NATIVE_ID = "11111111-2222-3333-4444-555555555555"
TOKEN = "integration-token"


class Stack:
    def __init__(self, server: Server, transcript: Path) -> None:
        self.server = server
        self.transcript = transcript
        self.base = f"http://127.0.0.1:{server.api.port}"
        self.ws_base = f"ws://127.0.0.1:{server.api.port}"


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    projects = tmp_path / "projects" / "f--home-dev-repo"
    projects.mkdir(parents=True)
    transcript = projects / f"{NATIVE_ID}.jsonl"
    shutil.copy(FIXTURE, transcript)

    settings = Settings(
        node_name="itest",
        data_dir=tmp_path / "data",
        api_host="127.0.0.1",
        api_port=0,
        api_token=TOKEN,
        adapters={
            "claude-code": {"projects_dir": str(tmp_path / "projects"), "poll_interval_s": 0.05}
        },
        discovery_interval_s=0.2,
        dashboard_dir=tmp_path / "no-dashboard",
    )
    server = Server(settings)
    await server.start()
    try:
        yield Stack(server, transcript)
    finally:
        await server.stop()


async def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def append_assistant_line(transcript: Path, text: str) -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "model": "claude-fable-5",
                "content": [{"type": "text", "text": text}],
            },
            "timestamp": "2026-07-12T11:00:00.000Z",
        }
    )
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


@pytest.mark.asyncio
async def test_running_claude_session_is_visible_and_streams_live(stack: Stack) -> None:
    async with httpx.AsyncClient(
        base_url=stack.base, headers={"Authorization": f"Bearer {TOKEN}"}
    ) as client:
        health = (await client.get("/api/health")).json()
        assert health["status"] == "ok"

        async def session_discovered() -> bool:
            sessions = (await client.get("/api/sessions")).json()["sessions"]
            return bool(sessions) and sessions[0]["state"] == "running"

        await wait_for(session_discovered)

        sessions = (await client.get("/api/sessions")).json()["sessions"]
        (session,) = sessions
        assert session["adapter"] == "claude-code"
        assert session["native_id"] == NATIVE_ID
        assert session["title"] == "Fix failing auth test"
        assert session["project"] == "/home/dev/repo"

        # the transcript history was replayed into the event log
        async def history_persisted() -> bool:
            body = (await client.get(f"/api/sessions/{session['id']}/events")).json()
            return any(e["type"] == "agent.output_appended" for e in body["events"])

        await wait_for(history_persisted)

        # live tail over WebSocket
        url = f"{stack.ws_base}/api/ws/events?token={TOKEN}&types=agent.*"
        async with websockets.connect(url) as ws:
            append_assistant_line(stack.transcript, "LIVE OVER WEBSOCKET")
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                assert remaining > 0, "live event did not arrive over WebSocket"
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                if (
                    frame["type"] == "agent.output_appended"
                    and frame["payload"]["text"] == "LIVE OVER WEBSOCKET"
                ):
                    assert frame["session_id"] == session["id"]
                    break

        # cursor replay: reconnect after the fact and catch up from the log
        events = (await client.get("/api/events", params={"limit": 1})).json()
        first_id = events["events"][0]["id"]
        replay_url = f"{stack.ws_base}/api/ws/events?token={TOKEN}&types=agent.*&after={first_id}"
        async with websockets.connect(replay_url) as ws:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert frame["type"].startswith("agent.")
            assert frame["id"] > first_id


@pytest.mark.asyncio
async def test_websocket_requires_token(stack: Stack) -> None:
    with pytest.raises(websockets.exceptions.WebSocketException):
        async with websockets.connect(f"{stack.ws_base}/api/ws/events"):
            pass  # pragma: no cover - the handshake must fail
