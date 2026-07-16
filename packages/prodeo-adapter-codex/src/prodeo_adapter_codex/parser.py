"""Versioned parser for Codex CLI rollout files (JSONL).

Rollout files (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``) are Codex's
own resume/replay format, not a public contract, so this module is the single
place that knows it (ADR-0004 posture). Each line is
``{"timestamp": ..., "type": ..., "payload": {...}}``; types pinned against
Codex CLI ~0.1x rollouts:

- ``session_meta`` — id, cwd, cli_version, originator, git info.
- ``turn_context`` — per-turn model/cwd/policy context.
- ``response_item`` — the conversation: messages, function/shell calls and
  their outputs, reasoning (skipped).
- ``event_msg`` — UI events. Only ``task_started``/``task_complete``/``error``
  are surfaced (message content arrives via ``response_item``; surfacing both
  would double every message).
- ``compacted`` — history compaction bookkeeping, skipped.

Unknown record types become opaque ``system`` output observations, so upstream
format drift degrades gracefully instead of failing.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from prodeo.adapters.observations import (
    Observation,
    OutputObservation,
    ToolObservation,
    ToolPhase,
    TurnObservation,
    TurnPhase,
)

PARSER_VERSION = 1

_MAX_DETAIL = 500
_MAX_TITLE = 80

#: User-message bodies Codex injects for its own context, not typed by a human.
_SYNTHETIC_USER_PREFIXES = ("<environment_context>", "<user_instructions>", "<turn_context>")

_SKIPPED_EVENT_MSGS = frozenset(
    {
        "agent_message",
        "agent_message_delta",
        "agent_reasoning",
        "agent_reasoning_delta",
        "user_message",
        "token_count",
        "exec_command_begin",
        "exec_command_output_delta",
        "exec_command_end",
    }
)


@dataclass
class RolloutMeta:
    """Descriptive session fields accumulated while parsing."""

    session_id: str = ""
    title: str = ""
    project: str = ""
    model: str = ""
    git_branch: str = ""
    agent_version: str = ""


@dataclass
class RolloutParser:
    """Stateful line-by-line parser for one rollout file."""

    native_id: str
    meta: RolloutMeta = field(default_factory=RolloutMeta)
    _meta_dirty: bool = False
    _pending_tools: dict[str, str] = field(default_factory=dict)

    def consume_meta_dirty(self) -> bool:
        """True once per batch of metadata changes."""
        dirty, self._meta_dirty = self._meta_dirty, False
        return dirty

    def feed_line(self, line: str) -> list[Observation]:
        line = line.strip()
        if not line:
            return []
        try:
            record: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []  # partial or corrupt line; the tailer only feeds whole lines
        if not isinstance(record, dict):
            return []
        return self._feed_record(record)

    def _feed_record(self, record: dict[str, Any]) -> list[Observation]:
        rtype = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        at = _timestamp(record)

        if rtype == "session_meta":
            self._absorb_session_meta(payload)
            return []
        if rtype == "turn_context":
            if (model := payload.get("model")) and self.meta.model != model:
                self.meta.model = str(model)
                self._meta_dirty = True
            return []
        if rtype == "response_item":
            return self._parse_response_item(payload, at)
        if rtype == "event_msg":
            return self._parse_event_msg(payload, at)
        if rtype == "compacted":
            return []
        # Unknown record type: surface it opaquely rather than fail (ADR-0004).
        return [
            OutputObservation(
                native_id=self.native_id,
                role="system",
                text=_clip(json.dumps(record), _MAX_DETAIL),
                at=at,
                metadata={"record_type": str(rtype), "opaque": True},
            )
        ]

    # ------------------------------------------------------------- records

    def _parse_response_item(
        self, payload: dict[str, Any], at: datetime | None
    ) -> list[Observation]:
        itype = payload.get("type")
        if itype == "message":
            return self._parse_message(payload, at)
        if itype == "function_call":
            call_id = str(payload.get("call_id") or "")
            name = str(payload.get("name") or "unknown")
            self._pending_tools[call_id] = name
            return [
                ToolObservation(
                    native_id=self.native_id,
                    phase=ToolPhase.STARTED,
                    tool=name,
                    tool_use_id=call_id,
                    detail=_clip(str(payload.get("arguments") or ""), _MAX_DETAIL),
                    at=at,
                )
            ]
        if itype == "function_call_output":
            call_id = str(payload.get("call_id") or "")
            output = payload.get("output")
            failed, detail = _interpret_output(output)
            return [
                ToolObservation(
                    native_id=self.native_id,
                    phase=ToolPhase.FAILED if failed else ToolPhase.FINISHED,
                    tool=self._pending_tools.pop(call_id, "unknown"),
                    tool_use_id=call_id,
                    detail=_clip(detail, _MAX_DETAIL),
                    at=at,
                )
            ]
        if itype == "local_shell_call":
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            command = (payload.get("action") or {}).get("command")
            detail = " ".join(map(str, command)) if isinstance(command, list) else ""
            self._pending_tools[call_id] = "shell"
            return [
                ToolObservation(
                    native_id=self.native_id,
                    phase=ToolPhase.STARTED,
                    tool="shell",
                    tool_use_id=call_id,
                    detail=_clip(detail, _MAX_DETAIL),
                    at=at,
                )
            ]
        if itype in ("custom_tool_call", "custom_tool_call_output"):
            call_id = str(payload.get("call_id") or "")
            if itype == "custom_tool_call":
                name = str(payload.get("name") or "unknown")
                self._pending_tools[call_id] = name
                detail = str(payload.get("input") or "")
                phase = ToolPhase.STARTED
            else:
                name = self._pending_tools.pop(call_id, "unknown")
                detail = str(payload.get("output") or "")
                phase = ToolPhase.FINISHED
            return [
                ToolObservation(
                    native_id=self.native_id,
                    phase=phase,
                    tool=name,
                    tool_use_id=call_id,
                    detail=_clip(detail, _MAX_DETAIL),
                    at=at,
                )
            ]
        # reasoning, web_search_call bookkeeping, and friends stay internal.
        return []

    def _parse_message(self, payload: dict[str, Any], at: datetime | None) -> list[Observation]:
        role = str(payload.get("role") or "")
        if role not in ("user", "assistant"):
            return []  # system/developer messages are Codex's own plumbing
        texts: list[str] = []
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") in (
                "input_text",
                "output_text",
                "text",
            ):
                texts.append(str(block.get("text") or ""))
        text = "\n".join(t for t in texts if t.strip())
        if not text:
            return []
        if role == "user":
            if text.lstrip().startswith(_SYNTHETIC_USER_PREFIXES):
                return []
            if not self.meta.title:
                self.meta.title = _clip(" ".join(text.split()), _MAX_TITLE)
                self._meta_dirty = True
        return [OutputObservation(native_id=self.native_id, role=role, text=text, at=at)]

    def _parse_event_msg(self, payload: dict[str, Any], at: datetime | None) -> list[Observation]:
        etype = payload.get("type")
        if etype == "task_started":
            return [TurnObservation(native_id=self.native_id, phase=TurnPhase.STARTED, at=at)]
        if etype == "task_complete":
            return [TurnObservation(native_id=self.native_id, phase=TurnPhase.COMPLETED, at=at)]
        if etype == "error":
            message = str(payload.get("message") or "")
            if message:
                return [
                    OutputObservation(
                        native_id=self.native_id,
                        role="system",
                        text=_clip(message, _MAX_DETAIL),
                        at=at,
                        metadata={"error": True},
                    )
                ]
            return []
        if etype in _SKIPPED_EVENT_MSGS:
            return []
        return []  # other UI events are noise for observation purposes

    # ---------------------------------------------------------------- meta

    def _absorb_session_meta(self, payload: dict[str, Any]) -> None:
        # Some versions nest the meta under "meta"; accept both shapes.
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
        assert isinstance(meta, dict)
        if (sid := meta.get("id")) and self.meta.session_id != sid:
            self.meta.session_id = str(sid)
            self._meta_dirty = True
        if (cwd := meta.get("cwd")) and self.meta.project != cwd:
            self.meta.project = str(cwd)
            self._meta_dirty = True
        if (version := meta.get("cli_version")) and self.meta.agent_version != version:
            self.meta.agent_version = str(version)
            self._meta_dirty = True
        git = meta.get("git")
        branch = git.get("branch") if isinstance(git, dict) else None
        if branch and self.meta.git_branch != branch:
            self.meta.git_branch = str(branch)
            self._meta_dirty = True


def _interpret_output(output: object) -> tuple[bool, str]:
    """function_call_output payloads are strings, sometimes JSON-encoded with
    an exit code; failures are best-effort detected, never guessed loudly."""
    if isinstance(output, dict):
        content = str(output.get("content") or output.get("output") or "")
        success = output.get("success")
        return (success is False), content
    if not isinstance(output, str):
        return False, ""
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, output
    if isinstance(parsed, dict):
        exit_code = (parsed.get("metadata") or {}).get("exit_code")
        text = str(parsed.get("output") or "")
        return (isinstance(exit_code, int) and exit_code != 0), text or output
    return False, output


def _timestamp(record: dict[str, Any]) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
