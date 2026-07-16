"""Codex CLI adapter: observe + historical via rollout files.

Codex CLI persists every session as an append-only JSONL "rollout" under
``~/.codex/sessions/YYYY/MM/DD/`` — a self-describing record stream with a
``session_meta`` header (which is why Codex was chosen as the third adapter;
see ADR-0009). Discovery scans the date-sharded tree; ``watch`` tails one
rollout through the versioned parser. Per-session byte offsets persist in the
adapter's data directory so restarts do not replay history.

Observe-only for now: Codex's control surfaces (proto/app-server modes) are
session-launch-time choices we cannot attach to after the fact; launching
Codex runs from Command Center is future work, and the capability flags are
honest about it. All file IO happens in threads (async discipline).
"""

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
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
from prodeo_adapter_codex.parser import RolloutParser

VERSION = "0.1.0"

_PEEK_BYTES = 16 * 1024  # the session_meta header is the first line


@dataclass
class _RolloutFile:
    path: Path
    native_id: str
    mtime: float
    size: int


class CodexAdapter(ObserveOnlyAdapter):
    """One session per rollout file under the Codex sessions directory."""

    def __init__(self) -> None:
        self.metadata = AdapterMetadata(name="codex", version=VERSION)
        self.capabilities = AdapterCapabilities(observe=True, historical_sessions=True)
        self._ctx: AdapterContext | None = None
        self._sessions_dir = Path.home() / ".codex" / "sessions"
        self._poll_interval = 1.0
        self._idle_timeout = 1800.0
        self._max_replay_bytes = 512 * 1024
        self._offsets: dict[str, int] = {}
        self._paths: dict[str, Path] = {}
        self._peek_cache: dict[str, tuple[float, SessionDescriptor]] = {}
        self._stopped = False

    # ----------------------------------------------------------- lifecycle

    async def start(self, ctx: AdapterContext) -> None:
        self._ctx = ctx
        cfg = ctx.config
        if raw_dir := cfg.get("sessions_dir"):
            self._sessions_dir = Path(str(raw_dir)).expanduser()
        self._poll_interval = float(cfg.get("poll_interval_s", self._poll_interval))
        self._idle_timeout = float(cfg.get("idle_timeout_s", self._idle_timeout))
        self._max_replay_bytes = int(cfg.get("max_replay_bytes", self._max_replay_bytes))
        self._stopped = False
        self._offsets = await asyncio.to_thread(self._load_offsets)
        ctx.logger.info("codex.started", sessions_dir=str(self._sessions_dir))

    async def stop(self) -> None:
        self._stopped = True

    # ----------------------------------------------------------- discovery

    async def discover_sessions(self) -> list[SessionDescriptor]:
        files = await asyncio.to_thread(self._scan)
        descriptors: list[SessionDescriptor] = []
        for f in files:
            self._paths[f.native_id] = f.path
            cached = self._peek_cache.get(f.native_id)
            if cached is not None and cached[0] == f.mtime:
                descriptors.append(cached[1])
                continue
            desc = await asyncio.to_thread(self._describe, f)
            self._peek_cache[f.native_id] = (f.mtime, desc)
            descriptors.append(desc)
        return descriptors

    def _scan(self) -> list[_RolloutFile]:
        if not self._sessions_dir.is_dir():
            return []
        found: list[_RolloutFile] = []
        # Date-sharded tree: sessions/YYYY/MM/DD/rollout-*.jsonl
        for path in self._sessions_dir.glob("*/*/*/rollout-*.jsonl"):
            with contextlib.suppress(OSError):
                stat = path.stat()
                found.append(
                    _RolloutFile(
                        path=path, native_id=path.stem, mtime=stat.st_mtime, size=stat.st_size
                    )
                )
        return found

    def _describe(self, f: _RolloutFile) -> SessionDescriptor:
        parser = RolloutParser(native_id=f.native_id)
        with contextlib.suppress(OSError):
            with f.path.open("rb") as fh:
                head = fh.read(_PEEK_BYTES)
            for line in head.decode("utf-8", errors="replace").splitlines():
                parser.feed_line(line)
        return self._descriptor(parser, mtime=f.mtime, path=f.path)

    def _descriptor(self, parser: RolloutParser, *, mtime: float, path: Path) -> SessionDescriptor:
        meta = parser.meta
        idle = time.time() - mtime
        state = SessionState.RUNNING if idle < self._idle_timeout else SessionState.COMPLETED
        return SessionDescriptor(
            native_id=parser.native_id,
            title=meta.title,
            project=meta.project,
            model=meta.model or None,
            state=state,
            last_activity_at=datetime.fromtimestamp(mtime, tz=UTC),
            metadata={
                k: v
                for k, v in {
                    "codex_session_id": meta.session_id,
                    "git_branch": meta.git_branch,
                    "agent_version": meta.agent_version,
                    "rollout": str(path),
                }.items()
                if v
            },
        )

    # --------------------------------------------------------------- watch

    async def watch(self, session: SessionRef) -> None:
        assert self._ctx is not None, "watch() before start()"
        ctx = self._ctx
        native_id = session.native_id
        path = self._paths.get(native_id)
        if path is None:
            await self.discover_sessions()
            path = self._paths.get(native_id)
        if path is None:
            await ctx.report(
                StateObservation(
                    native_id=native_id, state=SessionState.STOPPED, reason="rollout_missing"
                )
            )
            return

        parser = RolloutParser(native_id=native_id)
        offset = self._offsets.get(native_id)
        if offset is None:
            offset = await asyncio.to_thread(self._initial_offset, path)
        remainder = b""
        caught_up = False

        while not self._stopped:
            try:
                stat = await asyncio.to_thread(path.stat)
            except OSError:
                await ctx.report(
                    StateObservation(
                        native_id=native_id, state=SessionState.STOPPED, reason="rollout_removed"
                    )
                )
                return

            if stat.st_size < offset:  # truncated/rewritten: start over
                offset, remainder = 0, b""
            if stat.st_size > offset:
                chunk = await asyncio.to_thread(_read_range, path, offset, stat.st_size)
                offset += len(chunk)
                lines, remainder = _split_lines(remainder + chunk)
                fresh = caught_up
                for line in lines:
                    for obs in parser.feed_line(line):
                        await ctx.report(obs)
                if parser.consume_meta_dirty():
                    await ctx.report(
                        SessionObservation(
                            descriptor=self._descriptor(parser, mtime=stat.st_mtime, path=path)
                        )
                    )
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
                if time.time() - stat.st_mtime > self._idle_timeout:
                    await ctx.report(
                        StateObservation(
                            native_id=native_id, state=SessionState.COMPLETED, reason="idle"
                        )
                    )
                    return
            await asyncio.sleep(self._poll_interval)

    def _initial_offset(self, path: Path) -> int:
        """First-ever watch: replay history, but cap it for huge rollouts."""
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
