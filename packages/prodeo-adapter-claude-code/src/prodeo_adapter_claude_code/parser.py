"""Versioned parser for Claude Code session transcripts (JSONL).

The format is not a public contract (ADR-0004), so this module is the single
place that knows it. Rules:

- Known noise record types are skipped outright.
- Sidechain records (subagent traffic) and meta user records are skipped.
- Unknown record types become opaque ``system`` output observations - drift
  in the upstream format degrades gracefully instead of failing.

Record types this parser was pinned against (Claude Code ~2.x transcripts):
``user``, ``assistant``, ``ai-title``, ``summary``, ``attachment``,
``last-prompt``, ``queue-operation``, ``file-history-snapshot``.
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

#: Record types we understand and deliberately do not surface.
_SKIP_TYPES = frozenset({"attachment", "last-prompt", "queue-operation", "file-history-snapshot"})

_MAX_DETAIL = 500
_MAX_TITLE = 80


@dataclass
class TranscriptMeta:
    """Descriptive session fields accumulated while parsing."""

    title: str = ""
    project: str = ""
    model: str = ""
    git_branch: str = ""
    agent_version: str = ""
    last_timestamp: datetime | None = None


@dataclass
class TranscriptParser:
    """Stateful line-by-line parser for one session transcript."""

    native_id: str
    meta: TranscriptMeta = field(default_factory=TranscriptMeta)
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
        if rtype in _SKIP_TYPES or record.get("isSidechain") or record.get("isMeta"):
            return []
        at = _timestamp(record)
        if at is not None:
            self.meta.last_timestamp = at
        self._absorb_meta(record)

        if rtype == "ai-title":
            self._set_title(str(record.get("aiTitle") or ""), authoritative=True)
            return []
        if rtype == "summary":
            self._set_title(str(record.get("summary") or ""), authoritative=False)
            return []
        if rtype == "user":
            return self._parse_user(record, at)
        if rtype == "assistant":
            return self._parse_assistant(record, at)
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

    def _parse_user(self, record: dict[str, Any], at: datetime | None) -> list[Observation]:
        message = record.get("message") or {}
        content = message.get("content")
        out: list[Observation] = []
        if isinstance(content, str):
            if content.strip():
                out.append(
                    TurnObservation(native_id=self.native_id, phase=TurnPhase.STARTED, at=at)
                )
                out.append(
                    OutputObservation(native_id=self.native_id, role="user", text=content, at=at)
                )
            return out
        if not isinstance(content, list):
            return out
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(str(block.get("text") or ""))
            elif btype == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                failed = bool(block.get("is_error"))
                out.append(
                    ToolObservation(
                        native_id=self.native_id,
                        phase=ToolPhase.FAILED if failed else ToolPhase.FINISHED,
                        tool=self._pending_tools.pop(tool_use_id, "unknown"),
                        tool_use_id=tool_use_id,
                        detail=_clip(_result_text(block.get("content")), _MAX_DETAIL),
                        at=at,
                    )
                )
        text = "\n".join(t for t in texts if t.strip())
        if text:
            out.insert(0, TurnObservation(native_id=self.native_id, phase=TurnPhase.STARTED, at=at))
            out.append(OutputObservation(native_id=self.native_id, role="user", text=text, at=at))
        return out

    def _parse_assistant(self, record: dict[str, Any], at: datetime | None) -> list[Observation]:
        message = record.get("message") or {}
        if (model := message.get("model")) and self.meta.model != model:
            self.meta.model = str(model)
            self._meta_dirty = True
        out: list[Observation] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text") or "")
                if text.strip():
                    out.append(
                        OutputObservation(
                            native_id=self.native_id, role="assistant", text=text, at=at
                        )
                    )
            elif btype == "tool_use":
                tool_use_id = str(block.get("id") or "")
                name = str(block.get("name") or "unknown")
                self._pending_tools[tool_use_id] = name
                out.append(
                    ToolObservation(
                        native_id=self.native_id,
                        phase=ToolPhase.STARTED,
                        tool=name,
                        tool_use_id=tool_use_id,
                        detail=_clip(json.dumps(block.get("input") or {}), _MAX_DETAIL),
                        at=at,
                    )
                )
            # thinking blocks are deliberately not surfaced
        if message.get("stop_reason") in ("end_turn", "stop_sequence"):
            out.append(TurnObservation(native_id=self.native_id, phase=TurnPhase.COMPLETED, at=at))
        return out

    # ---------------------------------------------------------------- meta

    def _absorb_meta(self, record: dict[str, Any]) -> None:
        if (cwd := record.get("cwd")) and self.meta.project != cwd:
            self.meta.project = str(cwd)
            self._meta_dirty = True
        if (branch := record.get("gitBranch")) and self.meta.git_branch != branch:
            self.meta.git_branch = str(branch)
            self._meta_dirty = True
        if (version := record.get("version")) and self.meta.agent_version != version:
            self.meta.agent_version = str(version)
            self._meta_dirty = True
        if record.get("type") == "user" and not self.meta.title:
            content = (record.get("message") or {}).get("content")
            if isinstance(content, str) and content.strip():
                self._set_title(content, authoritative=False)

    def _set_title(self, raw: str, *, authoritative: bool) -> None:
        title = _clip(" ".join(raw.split()), _MAX_TITLE)
        if title and (authoritative or not self.meta.title) and self.meta.title != title:
            self.meta.title = title
            self._meta_dirty = True


def _timestamp(record: dict[str, Any]) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _result_text(content: object) -> str:
    """tool_result content is either a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
