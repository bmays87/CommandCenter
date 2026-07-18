"""The PermissionRequest hook CLI: presence gating, mediation, fail-open.

Every path must end in exit 0 — Claude Code treats a non-zero exit as a deny,
and this hook must never deny on its own behalf.
"""

import io
import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

import httpx
import pytest

from prodeo_adapter_claude_code import hook
from prodeo_adapter_claude_code.format import permission_prompt

TOOL_INPUT = {"command": "rm -rf build"}

AWAY = {"PRODEO_API_TOKEN": "t0ken"}


def stdin_payload(**overrides: object) -> IO[str]:
    data: dict[str, object] = {
        "session_id": "sess-1",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": dict(TOOL_INPUT),
        "cwd": "/p",
    }
    data.update(overrides)
    return io.StringIO(json.dumps(data))


def resolution(status: str = "answered", answer: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"interaction_id": "i1", "status": status, "answer": answer}


def transport_returning(
    body: dict[str, Any], requests: list[httpx.Request], status_code: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body)

    return httpx.MockTransport(handler)


def run_hook(
    stdin: IO[str],
    transport: httpx.BaseTransport,
    *,
    env: dict[str, str] | None = None,
    since_input: Callable[[], float | None] | None = None,
) -> tuple[int, str]:
    stdout = io.StringIO()
    code = hook.run(
        stdin,
        stdout,
        AWAY if env is None else env,
        transport=transport,
        since_input=since_input or (lambda: None),  # unknown = away = mediate
    )
    return code, stdout.getvalue()


def test_away_permission_maps_stdin_to_request_and_allow_output() -> None:
    requests: list[httpx.Request] = []
    transport = transport_returning(resolution(answer={"decision": "allow"}), requests)

    code, out = run_hook(stdin_payload(), transport)

    assert code == 0
    (request,) = requests
    assert request.url.path == "/api/interactions/external"
    assert request.headers["Authorization"] == "Bearer t0ken"
    sent = json.loads(request.content)
    title, body = permission_prompt("Bash", TOOL_INPUT)  # parity with the SDK path
    assert sent["adapter"] == "claude-code"
    assert sent["session_native_id"] == "sess-1"
    assert sent["kind"] == "permission"
    assert sent["title"] == title
    assert sent["body"] == body
    assert sent["native_id"].startswith("hook-")
    assert sent["timeout_s"] == hook.DEFAULT_TIMEOUT_S

    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PermissionRequest"
    assert decision["decision"] == {"behavior": "allow"}


def test_allow_with_updated_input_is_forwarded() -> None:
    transport = transport_returning(
        resolution(answer={"decision": "allow", "updated_input": {"command": "ls"}}), []
    )
    code, out = run_hook(stdin_payload(), transport)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "allow", "updatedInput": {"command": "ls"}}


def test_deny_carries_the_reason() -> None:
    transport = transport_returning(
        resolution(answer={"decision": "deny", "text": "not on prod"}), []
    )
    code, out = run_hook(stdin_payload(), transport)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "deny", "message": "not on prod"}


@pytest.mark.parametrize(
    "body",
    [
        resolution(status="timed_out"),
        resolution(status="cancelled"),
        resolution(answer=None),  # answered but malformed: no answer object
        {"unexpected": "shape"},
    ],
)
def test_non_answers_fall_through_to_the_terminal_prompt(body: dict[str, Any]) -> None:
    code, out = run_hook(stdin_payload(), transport_returning(body, []))
    assert code == 0
    assert out == ""


def test_http_error_falls_through() -> None:
    code, out = run_hook(stdin_payload(), transport_returning({}, [], status_code=500))
    assert (code, out) == (0, "")


def test_connect_error_falls_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server down")

    code, out = run_hook(stdin_payload(), httpx.MockTransport(handler))
    assert (code, out) == (0, "")


def test_managed_sessions_pass_through_without_a_request() -> None:
    requests: list[httpx.Request] = []
    transport = transport_returning(resolution(answer={"decision": "allow"}), requests)

    code, out = run_hook(stdin_payload(), transport, env={"PRODEO_MANAGED": "1", **AWAY})

    assert (code, out) == (0, "")
    assert requests == []  # can_use_tool already mediates launched sessions


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash"}),
        json.dumps({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}),
        json.dumps({"hook_event_name": "PermissionRequest", "session_id": "s", "tool_input": {}}),
        json.dumps([1, 2, 3]),
        "",
    ],
)
def test_malformed_or_foreign_stdin_passes_through(raw: str) -> None:
    requests: list[httpx.Request] = []
    transport = transport_returning(resolution(answer={"decision": "allow"}), requests)
    code, out = run_hook(io.StringIO(raw), transport)
    assert (code, out) == (0, "")
    assert requests == []


def test_recent_local_input_passes_through_without_a_request() -> None:
    requests: list[httpx.Request] = []
    transport = transport_returning(resolution(answer={"decision": "allow"}), requests)

    code, out = run_hook(stdin_payload(), transport, since_input=lambda: 5.0)

    assert (code, out) == (0, "")
    assert requests == []  # at the station: the terminal prompt wins instantly


def test_input_resuming_mid_poll_aborts_and_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook, "POLL_INTERVAL_S", 0.05)
    release = threading.Event()
    requests: list[httpx.Request] = []

    def hanging_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        release.wait(timeout=10)
        return httpx.Response(200, json=resolution(answer={"decision": "allow"}))

    def returning_user() -> Iterator[float | None]:
        yield 1000.0  # the initial gate: away
        while True:
            yield 2.0  # then the keyboard is touched

    presence = returning_user()
    try:
        code, out = run_hook(
            stdin_payload(),
            httpx.MockTransport(hanging_handler),
            since_input=lambda: next(presence),
        )
    finally:
        release.set()

    assert (code, out) == (0, "")  # no decision: the terminal prompt wins
    assert len(requests) == 1  # the mediation request was made, then abandoned


# ------------------------------------------------------------- installation


def test_print_config_snippet_registers_this_interpreter() -> None:
    snippet = hook.settings_snippet()
    (group,) = snippet["hooks"]["PermissionRequest"]
    (entry,) = group["hooks"]
    assert entry["type"] == "command"
    assert "prodeo_adapter_claude_code.hook" in entry["command"]
    json.dumps(snippet)  # must be settings.json-pastable


def test_install_creates_settings_file(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    message = hook.install(settings_path)

    assert "installed" in message
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    (group,) = settings["hooks"]["PermissionRequest"]
    assert "prodeo_adapter_claude_code.hook" in group["hooks"][0]["command"]
    assert not list(tmp_path.glob("*.bak-*"))  # nothing existed to back up


def test_install_merges_preserving_unrelated_keys_with_backup(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    existing = {
        "permissions": {"allow": ["Bash(ls *)"]},
        "hooks": {
            "PermissionRequest": [{"hooks": [{"type": "command", "command": "other-hook"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
        },
    }
    settings_path.write_text(json.dumps(existing), encoding="utf-8")

    hook.install(settings_path)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["permissions"] == {"allow": ["Bash(ls *)"]}
    assert settings["hooks"]["Stop"] == existing["hooks"]["Stop"]
    groups = settings["hooks"]["PermissionRequest"]
    assert groups[0]["hooks"][0]["command"] == "other-hook"
    assert "prodeo_adapter_claude_code.hook" in groups[1]["hooks"][0]["command"]
    (backup,) = tmp_path.glob("settings.json.bak-*")
    assert json.loads(backup.read_text(encoding="utf-8")) == existing


def test_install_is_idempotent(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    hook.install(settings_path)

    message = hook.install(settings_path)

    assert "already installed" in message
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert len(settings["hooks"]["PermissionRequest"]) == 1
    assert len(list(tmp_path.glob("*.bak-*"))) == 0
