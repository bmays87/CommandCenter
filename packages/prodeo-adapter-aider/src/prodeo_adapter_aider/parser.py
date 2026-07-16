"""Versioned parser for Aider chat history files (markdown).

``.aider.chat.history.md`` is a human-facing log, not a public contract, so
this module is the single place that knows its shape (the same posture as the
claude-code adapter, ADR-0004). The format, pinned against Aider ~0.8x:

- ``# aider chat started at YYYY-MM-DD HH:MM:SS`` — a new run in this project.
- ``#### <text>`` — one line of a user prompt (consecutive lines accumulate).
- ``> <text>`` — informational lines: version, model, git repo, applied
  edits, token/cost accounting.
- everything else — the assistant's markdown response.

Because the file is markdown rather than a record stream, output is buffered:
user prompts flush when the prompt block ends, assistant text flushes when a
structural line arrives *or* when the adapter calls :meth:`flush` (the file
has gone quiet). Unrecognized structure degrades to assistant text — drift in
the upstream format garbles cosmetics, never crashes the watch.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from prodeo.adapters.observations import (
    Observation,
    OutputObservation,
    ToolObservation,
    ToolPhase,
    TurnObservation,
    TurnPhase,
)

PARSER_VERSION = 1

_MAX_TITLE = 80

_CHAT_STARTED = re.compile(r"^# aider chat started at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_MODEL_LINE = re.compile(r"^Model: (\S+)")
_VERSION_LINE = re.compile(r"^Aider v(\S+)")
_APPLIED_EDIT = re.compile(r"^Applied edit to (.+)")
_TOKENS_LINE = re.compile(r"^Tokens: ")


@dataclass
class HistoryMeta:
    """Descriptive session fields accumulated while parsing."""

    title: str = ""
    model: str = ""
    agent_version: str = ""
    last_started_at: datetime | None = None


@dataclass
class HistoryParser:
    """Stateful line-by-line parser for one project's chat history."""

    native_id: str
    meta: HistoryMeta = field(default_factory=HistoryMeta)
    _meta_dirty: bool = False
    _user_buffer: list[str] = field(default_factory=list)
    _assistant_buffer: list[str] = field(default_factory=list)
    _edit_counter: int = 0

    def consume_meta_dirty(self) -> bool:
        """True once per batch of metadata changes."""
        dirty, self._meta_dirty = self._meta_dirty, False
        return dirty

    def feed_line(self, line: str) -> list[Observation]:
        line = line.rstrip("\n")

        if started := _CHAT_STARTED.match(line):
            out = self.flush()
            self.meta.last_started_at = _parse_local_ts(started.group(1))
            self._meta_dirty = True
            return out

        if line.startswith("#### "):
            out = self._flush_assistant()
            self._user_buffer.append(line[5:])
            return out
        if line == "####":  # a blank line inside a prompt block
            self._user_buffer.append("")
            return []

        if line.startswith("> "):
            out = self.flush()
            out.extend(self._parse_info(line[2:].strip()))
            return out
        if line == ">":
            return self.flush()

        # Anything else is assistant markdown; a non-#### line also ends a
        # pending user prompt block.
        out = self._flush_user()
        if line.strip() or self._assistant_buffer:
            self._assistant_buffer.append(line)
        return out

    def flush(self) -> list[Observation]:
        """Emit buffered output; the adapter calls this when the file idles."""
        return self._flush_user() + self._flush_assistant()

    # ------------------------------------------------------------ internal

    def _parse_info(self, text: str) -> list[Observation]:
        if not text:
            return []
        if version := _VERSION_LINE.match(text):
            if self.meta.agent_version != version.group(1):
                self.meta.agent_version = version.group(1)
                self._meta_dirty = True
            return []
        if model := _MODEL_LINE.match(text):
            if self.meta.model != model.group(1):
                self.meta.model = model.group(1)
                self._meta_dirty = True
            return []
        if edit := _APPLIED_EDIT.match(text):
            self._edit_counter += 1
            return [
                ToolObservation(
                    native_id=self.native_id,
                    phase=ToolPhase.FINISHED,
                    tool="edit",
                    tool_use_id=f"edit-{self._edit_counter}",
                    detail=edit.group(1),
                )
            ]
        if _TOKENS_LINE.match(text):
            # The accounting line closes the assistant's turn.
            return [TurnObservation(native_id=self.native_id, phase=TurnPhase.COMPLETED)]
        # Other info lines (git repo, warnings, command output) are context.
        return [OutputObservation(native_id=self.native_id, role="system", text=text)]

    def _flush_user(self) -> list[Observation]:
        if not self._user_buffer:
            return []
        text = "\n".join(self._user_buffer).strip()
        self._user_buffer.clear()
        if not text:
            return []
        if not self.meta.title:
            self.meta.title = _clip(" ".join(text.split()), _MAX_TITLE)
            self._meta_dirty = True
        return [
            TurnObservation(native_id=self.native_id, phase=TurnPhase.STARTED),
            OutputObservation(native_id=self.native_id, role="user", text=text),
        ]

    def _flush_assistant(self) -> list[Observation]:
        if not self._assistant_buffer:
            return []
        text = "\n".join(self._assistant_buffer).strip()
        self._assistant_buffer.clear()
        if not text:
            return []
        return [OutputObservation(native_id=self.native_id, role="assistant", text=text)]


def _parse_local_ts(raw: str) -> datetime:
    """Aider writes naive local timestamps; interpret them as such."""
    local_tz = datetime.now(UTC).astimezone().tzinfo
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_tz)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
