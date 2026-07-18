"""``prodeo-claude-hook``: presence-gated PermissionRequest mediation (ADR-0011).

Claude Code invokes this at permission-prompt time. If the user recently
touched this machine, it exits immediately and the normal terminal prompt
wins. Otherwise it submits the permission to Command Center's external
interaction API and blocks until a human answers remotely (dashboard, voice,
phone), the interaction times out, or local input resumes — the latter two
fall through to the terminal prompt.

Posture is strictly fail-open: any error — server down, bad response, bug —
means exit 0 with no output, which shows the normal prompt. This process must
never exit non-zero (Claude Code treats exit 2 as a deny).
"""

import argparse
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import httpx
from ulid import ULID

from prodeo_adapter_claude_code.format import permission_prompt
from prodeo_adapter_claude_code.presence import SinceInputFn, seconds_since_input

ADAPTER = "claude-code"
DEFAULT_SERVER_URL = "http://127.0.0.1:8600"
DEFAULT_TIMEOUT_S = 570.0  # < Claude Code's 600s hook cap, with margin
DEFAULT_PRESENT_THRESHOLD_S = 90.0
#: The server resolves at ``timeout_s``; the HTTP read timeout sits above it
#: so the response (not a client-side timeout) always arrives first.
HTTP_GRACE_S = 15.0
POLL_INTERVAL_S = 1.0

ClockFn = Callable[[], float]


def _passthrough() -> int:
    """No output, exit 0: Claude Code falls through to its own prompt."""
    return 0


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key, ""))
    except ValueError:
        return default


def parse_permission_request(raw: str) -> dict[str, Any] | None:
    """The stdin payload, or None for anything that must pass through."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("hook_event_name") != "PermissionRequest":
        return None
    session_id = data.get("session_id")
    tool_name = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if not isinstance(tool_input, dict):
        return None
    return data


def decision_output(resolution: dict[str, Any]) -> dict[str, Any] | None:
    """Map the server's terminal interaction state to hook output.

    Only an explicit human answer produces output; timeout, cancellation, and
    anything malformed fall through to the terminal prompt.
    """
    if resolution.get("status") != "answered":
        return None
    answer = resolution.get("answer")
    if not isinstance(answer, dict):
        return None
    decision: dict[str, Any]
    if answer.get("decision") == "allow":
        decision = {"behavior": "allow"}
        updated_input = answer.get("updated_input")
        if isinstance(updated_input, dict):
            decision["updatedInput"] = updated_input
    elif answer.get("decision") == "deny":
        decision = {
            "behavior": "deny",
            "message": str(answer.get("text") or "denied via Command Center"),
        }
    else:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def run(
    stdin: IO[str],
    stdout: IO[str],
    env: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
    since_input: SinceInputFn | None = None,
    clock: ClockFn | None = None,
) -> int:
    """The hook body; every failure path resolves to passthrough (exit 0)."""
    try:
        return _run(stdin, stdout, env, transport, since_input, clock)
    except Exception:
        return _passthrough()


def _run(
    stdin: IO[str],
    stdout: IO[str],
    env: Mapping[str, str],
    transport: httpx.BaseTransport | None,
    since_input: SinceInputFn | None,
    clock: ClockFn | None,
) -> int:
    since_input = since_input or seconds_since_input
    clock = clock or time.monotonic

    if env.get("PRODEO_MANAGED") == "1":
        return _passthrough()  # SDK-launched: can_use_tool already mediates
    request = parse_permission_request(stdin.read())
    if request is None:
        return _passthrough()

    threshold = _env_float(env, "PRODEO_PRESENT_THRESHOLD_S", DEFAULT_PRESENT_THRESHOLD_S)
    elapsed = since_input()
    if elapsed is not None and elapsed < threshold:
        return _passthrough()  # at the station: the terminal prompt wins

    timeout_s = _env_float(env, "PRODEO_HOOK_TIMEOUT_S", DEFAULT_TIMEOUT_S)
    title, body = permission_prompt(str(request["tool_name"]), dict(request["tool_input"]))
    payload = {
        "adapter": ADAPTER,
        "session_native_id": request["session_id"],
        "kind": "permission",
        "title": title,
        "body": body,
        "native_id": f"hook-{ULID()}",
        "timeout_s": timeout_s,
    }
    server_url = env.get("PRODEO_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")
    headers = {}
    if token := env.get("PRODEO_API_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"

    client = httpx.Client(
        transport=transport,
        headers=headers,
        timeout=httpx.Timeout(10.0, read=timeout_s + HTTP_GRACE_S),
    )
    result: dict[str, Any] = {}

    def request_worker() -> None:
        try:
            response = client.post(f"{server_url}/api/interactions/external", json=payload)
            response.raise_for_status()
            result["resolution"] = response.json()
        except Exception:
            pass  # connect/read/HTTP errors and aborts all fall through

    worker = threading.Thread(target=request_worker, daemon=True)
    worker.start()
    deadline = clock() + timeout_s + HTTP_GRACE_S
    try:
        while worker.is_alive() and clock() < deadline:
            worker.join(POLL_INTERVAL_S)
            if not worker.is_alive():
                break
            elapsed = since_input()
            if elapsed is not None and elapsed < threshold:
                # Back at the station: abort the request (the server withdraws
                # the card on disconnect) and let the terminal prompt win.
                client.close()
                return _passthrough()
    finally:
        if not worker.is_alive():
            client.close()

    resolution = result.get("resolution")
    if not isinstance(resolution, dict):
        return _passthrough()
    output = decision_output(resolution)
    if output is None:
        return _passthrough()
    stdout.write(json.dumps(output))
    return 0


# ------------------------------------------------------------- installation


def hook_command() -> str:
    return f'"{sys.executable}" -m prodeo_adapter_claude_code.hook'


def settings_snippet() -> dict[str, Any]:
    return {
        "hooks": {
            "PermissionRequest": [{"hooks": [{"type": "command", "command": hook_command()}]}]
        }
    }


def install(settings_path: Path) -> str:
    """Merge the hook into ``settings_path``, idempotently, with a backup.

    Unrelated keys and existing hooks are preserved; a previous install (any
    interpreter path) is detected by the module marker in the command.
    """
    settings: dict[str, Any] = {}
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError(f"{settings_path} does not contain a JSON object")
    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault("PermissionRequest", [])
    for group in entries:
        for h in group.get("hooks", []):
            if "prodeo_adapter_claude_code.hook" in str(h.get("command", "")):
                return f"already installed in {settings_path}"
    if settings_path.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = settings_path.with_name(f"{settings_path.name}.bak-{stamp}")
        shutil.copy2(settings_path, backup)
    entries.append({"hooks": [{"type": "command", "command": hook_command()}]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return f"installed into {settings_path}"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="prodeo-claude-hook",
        description="Claude Code PermissionRequest hook mediated by Prodeo Command Center",
    )
    parser.add_argument(
        "--print-config", action="store_true", help="print the settings.json snippet and exit"
    )
    parser.add_argument(
        "--install", action="store_true", help="merge the hook into Claude Code settings"
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
        help="settings file for --install (default: ~/.claude/settings.json)",
    )
    args = parser.parse_args()
    if args.print_config:
        print(json.dumps(settings_snippet(), indent=2))
        return 0
    if args.install:
        print(install(args.settings))
        return 0
    return run(sys.stdin, sys.stdout, os.environ)


if __name__ == "__main__":
    sys.exit(main())
