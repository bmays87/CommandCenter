"""Interactive-session mediation, end to end (ADR-0011): the PermissionRequest
hook long-polls the real composed server, a human answers over REST, and the
hook emits the decision Claude Code expects. Presence is faked to "away";
the at-station and return-to-station paths need real local input and are
exercised manually on the Windows host (see the adapter README).
"""

import asyncio
import io
import json
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import IO, Any

import httpx
import pytest
import pytest_asyncio

from prodeo.config import Settings
from prodeo.server import Server
from prodeo_adapter_claude_code import hook

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parents[1] / "fixtures" / "claude-code-session.jsonl"
NATIVE_ID = "11111111-2222-3333-4444-555555555555"
TOKEN = "integration-token"

AWAY: Callable[[], float | None] = lambda: None  # noqa: E731 - unknown = mediate


class Stack:
    def __init__(self, server: Server) -> None:
        self.server = server
        self.base = f"http://127.0.0.1:{server.api.port}"


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    projects = tmp_path / "projects" / "f--home-dev-repo"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / f"{NATIVE_ID}.jsonl")

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
        yield Stack(server)
    finally:
        await server.stop()


def hook_stdin() -> IO[str]:
    return io.StringIO(
        json.dumps(
            {
                "session_id": NATIVE_ID,
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf build"},
                "cwd": "/home/dev/repo",
            }
        )
    )


def hook_env(stack: Stack, **extra: str) -> dict[str, str]:
    return {"PRODEO_SERVER_URL": stack.base, "PRODEO_API_TOKEN": TOKEN, **extra}


async def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_away_hook_mediates_and_carries_the_answer_back(stack: Stack) -> None:
    async with httpx.AsyncClient(
        base_url=stack.base, headers={"Authorization": f"Bearer {TOKEN}"}
    ) as client:
        stdout = io.StringIO()
        hook_run = asyncio.create_task(
            asyncio.to_thread(hook.run, hook_stdin(), stdout, hook_env(stack), since_input=AWAY)
        )

        async def pending_appeared() -> bool:
            body = (await client.get("/api/interactions", params={"status": "pending"})).json()
            return bool(body["interactions"])

        await wait_for(pending_appeared)
        body = (await client.get("/api/interactions", params={"status": "pending"})).json()
        (interaction,) = body["interactions"]
        assert interaction["adapter"] == "claude-code"
        assert interaction["title"] == "Allow Bash?"
        assert "rm -rf build" in interaction["body"]

        session = (await client.get(f"/api/sessions/{interaction['session_id']}")).json()
        assert session["native_id"] == NATIVE_ID
        assert session["state"] == "waiting_on_user"

        answered = await client.post(
            f"/api/interactions/{interaction['id']}/answer", json={"decision": "allow"}
        )
        assert answered.status_code == 200

        assert await asyncio.wait_for(hook_run, timeout=10) == 0
        decision = json.loads(stdout.getvalue())["hookSpecificOutput"]
        assert decision["hookEventName"] == "PermissionRequest"
        assert decision["decision"] == {"behavior": "allow"}


@pytest.mark.asyncio
async def test_unanswered_hook_times_out_to_the_terminal_prompt(stack: Stack) -> None:
    stdout = io.StringIO()

    code = await asyncio.wait_for(
        asyncio.to_thread(
            hook.run,
            hook_stdin(),
            stdout,
            hook_env(stack, PRODEO_HOOK_TIMEOUT_S="0.3"),
            since_input=AWAY,
        ),
        timeout=10,
    )

    assert code == 0
    assert stdout.getvalue() == ""  # no output: Claude Code shows its own prompt

    async with httpx.AsyncClient(
        base_url=stack.base, headers={"Authorization": f"Bearer {TOKEN}"}
    ) as client:
        body = (await client.get("/api/interactions", params={"status": "timed_out"})).json()
        assert len(body["interactions"]) == 1


@pytest.mark.asyncio
async def test_unreachable_server_fails_open_fast(stack: Stack) -> None:
    stdout = io.StringIO()
    env = {"PRODEO_SERVER_URL": "http://127.0.0.1:9", "PRODEO_API_TOKEN": TOKEN}

    start = asyncio.get_running_loop().time()
    code = await asyncio.wait_for(
        asyncio.to_thread(hook.run, hook_stdin(), stdout, env, since_input=AWAY),
        timeout=10,
    )

    assert code == 0
    assert stdout.getvalue() == ""
    assert asyncio.get_running_loop().time() - start < 5  # refused connection, not a hang
