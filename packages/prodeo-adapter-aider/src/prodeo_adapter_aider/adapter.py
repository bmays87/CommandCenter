"""Aider adapter: observe + historical via chat history files.

Aider appends a markdown log (``.aider.chat.history.md``) to each project it
runs in; there is no daemon, no socket, and no structured transcript — the
history file *is* the observable session format (which is exactly why Aider
was chosen as the second adapter; see ADR-0009). One project directory = one
session: repeated aider runs in the same project append to the same file and
surface as the session resuming.

Configure the projects to watch (``PRODEO_ADAPTERS``):

    {"aider": {"projects": ["/home/me/src/app", "/home/me/src/lib"]}}

Observe-only: Aider offers no remote control surface, so the capability flags
say so and the dashboard renders accordingly. Per-session byte offsets persist
in the adapter's data directory so restarts do not replay history into the
event log. All file IO happens in threads (async discipline).
"""

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from prodeo.adapters.context import AdapterContext
from prodeo.adapters.interface import (
    AdapterCapabilities,
    AdapterMetadata,
    ObserveOnlyAdapter,
    SessionRef,
)
from prodeo.adapters.observations import SessionObservation, StateObservation
from prodeo.sessions.model import SessionDescriptor
from prodeo.sessions.state import SessionState
from prodeo_adapter_aider.parser import HistoryParser

VERSION = "0.1.0"

DEFAULT_HISTORY_FILENAME = ".aider.chat.history.md"


class AiderAdapter(ObserveOnlyAdapter):
    """One session per configured project's chat history file."""

    def __init__(self) -> None:
        self.metadata = AdapterMetadata(name="aider", version=VERSION)
        self.capabilities = AdapterCapabilities(observe=True, historical_sessions=True)
        self._ctx: AdapterContext | None = None
        self._projects: list[Path] = []
        self._history_filename = DEFAULT_HISTORY_FILENAME
        self._poll_interval = 1.0
        self._idle_timeout = 1800.0
        self._max_replay_bytes = 256 * 1024
        self._offsets: dict[str, int] = {}
        self._stopped = False

    # ----------------------------------------------------------- lifecycle

    async def start(self, ctx: AdapterContext) -> None:
        self._ctx = ctx
        cfg = ctx.config
        self._projects = [Path(str(p)).expanduser() for p in cfg.get("projects", [])]
        self._history_filename = str(cfg.get("history_filename", self._history_filename))
        self._poll_interval = float(cfg.get("poll_interval_s", self._poll_interval))
        self._idle_timeout = float(cfg.get("idle_timeout_s", self._idle_timeout))
        self._max_replay_bytes = int(cfg.get("max_replay_bytes", self._max_replay_bytes))
        self._stopped = False
        self._offsets = await asyncio.to_thread(self._load_offsets)
        ctx.logger.info("aider.started", projects=len(self._projects))

    async def stop(self) -> None:
        self._stopped = True

    # ----------------------------------------------------------- discovery

    async def discover_sessions(self) -> list[SessionDescriptor]:
        return await asyncio.to_thread(self._scan)

    def _scan(self) -> list[SessionDescriptor]:
        found: list[SessionDescriptor] = []
        for project in self._projects:
            path = project / self._history_filename
            with contextlib.suppress(OSError):
                stat = path.stat()
                idle = time.time() - stat.st_mtime
                found.append(
                    SessionDescriptor(
                        native_id=str(project),
                        title=project.name,
                        project=str(project),
                        state=(
                            SessionState.RUNNING
                            if idle < self._idle_timeout
                            else SessionState.COMPLETED
                        ),
                        last_activity_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                        metadata={"history": str(path)},
                    )
                )
        return found

    # --------------------------------------------------------------- watch

    async def watch(self, session: SessionRef) -> None:
        assert self._ctx is not None, "watch() before start()"
        ctx = self._ctx
        native_id = session.native_id
        project = Path(native_id)
        path = project / self._history_filename

        parser = HistoryParser(native_id=native_id)
        offset = self._offsets.get(native_id)
        if offset is None:
            offset = await asyncio.to_thread(self._initial_offset, path)
        remainder = b""
        caught_up = False
        quiet_cycles = 0

        while not self._stopped:
            try:
                stat = await asyncio.to_thread(path.stat)
            except OSError:
                await ctx.report(
                    StateObservation(
                        native_id=native_id, state=SessionState.STOPPED, reason="history_removed"
                    )
                )
                return

            if stat.st_size < offset:  # truncated/rewritten: start over
                offset, remainder = 0, b""
            if stat.st_size > offset:
                quiet_cycles = 0
                chunk = await asyncio.to_thread(_read_range, path, offset, stat.st_size)
                offset += len(chunk)
                lines, remainder = _split_lines(remainder + chunk)
                fresh = caught_up
                for line in lines:
                    for obs in parser.feed_line(line):
                        await ctx.report(obs)
                if parser.consume_meta_dirty():
                    await ctx.report(SessionObservation(descriptor=self._descriptor(parser)))
                if fresh:
                    await ctx.report(
                        StateObservation(
                            native_id=native_id, state=SessionState.RUNNING, reason="activity"
                        )
                    )
                self._offsets[native_id] = offset - len(remainder)
                await asyncio.to_thread(self._save_offsets)
                caught_up = caught_up or offset >= stat.st_size
            else:
                caught_up = True
                quiet_cycles += 1
                if quiet_cycles == 2:  # the file settled: flush buffered markdown
                    for obs in parser.flush():
                        await ctx.report(obs)
                if time.time() - stat.st_mtime > self._idle_timeout:
                    await ctx.report(
                        StateObservation(
                            native_id=native_id, state=SessionState.COMPLETED, reason="idle"
                        )
                    )
                    return
            await asyncio.sleep(self._poll_interval)

    def _descriptor(self, parser: HistoryParser) -> SessionDescriptor:
        project = Path(parser.native_id)
        meta = parser.meta
        return SessionDescriptor(
            native_id=parser.native_id,
            title=meta.title or project.name,
            project=str(project),
            model=meta.model or None,
            last_activity_at=meta.last_started_at,
            metadata={
                k: v
                for k, v in {
                    "agent_version": meta.agent_version,
                    "history": str(project / self._history_filename),
                }.items()
                if v
            },
        )

    def _initial_offset(self, path: Path) -> int:
        """First-ever watch: replay history, but cap it for huge logs."""
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        if size <= self._max_replay_bytes:
            return 0
        start = size - self._max_replay_bytes
        with path.open("rb") as fh:
            fh.seek(start)
            fh.readline()  # align to the next whole line
            return fh.tell()

    # ------------------------------------------------------------- offsets

    def _offsets_path(self) -> Path:
        assert self._ctx is not None
        return self._ctx.data_dir / "offsets.json"

    def _load_offsets(self) -> dict[str, int]:
        try:
            raw = json.loads(self._offsets_path().read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in raw.items()}
        except (OSError, ValueError):
            return {}

    def _save_offsets(self) -> None:
        path = self._offsets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._offsets), encoding="utf-8")


def _read_range(path: Path, start: int, end: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(start)
        return fh.read(end - start)


def _split_lines(data: bytes) -> tuple[list[str], bytes]:
    """Complete decoded lines plus the trailing partial line (if any)."""
    if b"\n" not in data:
        return [], data
    whole, _, remainder = data.rpartition(b"\n")
    lines = whole.decode("utf-8", errors="replace").splitlines()
    return lines, remainder
