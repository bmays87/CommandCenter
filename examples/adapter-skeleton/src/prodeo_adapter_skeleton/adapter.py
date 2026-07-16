"""A complete observe-only adapter in ~150 lines.

The imaginary "agent" here is anything that appends to ``*.log`` files in a
directory: each file is one session, each appended line is agent output.
Replace the file-watching with your agent's real session source and you have
an adapter.

The parts every adapter has:

1. ``metadata`` / ``capabilities`` — declared, not assumed. This adapter
   observes and reports history; it declares nothing it cannot do, and the
   ``ObserveOnlyAdapter`` base makes the undeclared control methods refuse
   loudly (the conformance suite checks that).
2. ``start(ctx)`` / ``stop()`` — read validated config from ``ctx.config``,
   remember the ctx; never block the event loop (file IO goes through
   ``asyncio.to_thread``).
3. ``discover_sessions()`` — cheap, idempotent, called periodically. Returns
   descriptors keyed by *your* native id; the core assigns its own ids.
4. ``watch(session)`` — a long-running task per session, reporting typed
   observations via ``ctx.report(...)``. Return when the session is over;
   raise and the Adapter Manager restarts you with backoff.

What this example skips (see prodeo-adapter-claude-code for the real thing):
persistent read offsets (so a server restart does not replay old output into
the event log) and a versioned parser for structured transcripts.
"""

import asyncio
import contextlib
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
from prodeo.adapters.observations import OutputObservation, StateObservation
from prodeo.sessions.model import SessionDescriptor
from prodeo.sessions.state import SessionState


class SkeletonAdapter(ObserveOnlyAdapter):
    """One session per ``*.log`` file; one output observation per line."""

    def __init__(self) -> None:
        self.metadata = AdapterMetadata(name="skeleton", version="0.1.0")
        self.capabilities = AdapterCapabilities(observe=True, historical_sessions=True)
        self._ctx: AdapterContext | None = None
        self._logs_dir: Path | None = None
        self._poll_interval = 0.5
        self._idle_timeout = 300.0
        self._stopped = False

    # ---------------------------------------------------------- lifecycle

    async def start(self, ctx: AdapterContext) -> None:
        self._ctx = ctx
        # ctx.config is this adapter's slice of PRODEO_ADAPTERS, e.g.
        # PRODEO_ADAPTERS='{"skeleton": {"logs_dir": "/tmp/agent-logs"}}'.
        if raw := ctx.config.get("logs_dir"):
            self._logs_dir = Path(str(raw))
        self._poll_interval = float(ctx.config.get("poll_interval_s", self._poll_interval))
        self._idle_timeout = float(ctx.config.get("idle_timeout_s", self._idle_timeout))
        self._stopped = False
        ctx.logger.info("skeleton.started", logs_dir=str(self._logs_dir))

    async def stop(self) -> None:
        # Must be idempotent; watch tasks are cancelled by the manager.
        self._stopped = True

    # ---------------------------------------------------------- discovery

    async def discover_sessions(self) -> list[SessionDescriptor]:
        if self._logs_dir is None:
            return []  # unconfigured: an inert, but honest, adapter
        return await asyncio.to_thread(self._scan)

    def _scan(self) -> list[SessionDescriptor]:
        if self._logs_dir is None or not self._logs_dir.is_dir():
            return []
        found: list[SessionDescriptor] = []
        for path in sorted(self._logs_dir.glob("*.log")):
            with contextlib.suppress(OSError):
                stat = path.stat()
                idle = time.time() - stat.st_mtime
                found.append(
                    SessionDescriptor(
                        native_id=path.stem,
                        title=path.stem,
                        project=str(self._logs_dir),
                        state=(
                            SessionState.RUNNING
                            if idle < self._idle_timeout
                            else SessionState.COMPLETED
                        ),
                        last_activity_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    )
                )
        return found

    # -------------------------------------------------------------- watch

    async def watch(self, session: SessionRef) -> None:
        assert self._ctx is not None, "watch() before start()"
        ctx = self._ctx
        if self._logs_dir is None:
            return
        path = self._logs_dir / f"{session.native_id}.log"
        offset = 0
        while not self._stopped:
            try:
                stat = await asyncio.to_thread(path.stat)
            except OSError:
                # The session's backing file vanished: report and end the watch.
                await ctx.report(
                    StateObservation(
                        native_id=session.native_id,
                        state=SessionState.STOPPED,
                        reason="log_removed",
                    )
                )
                return
            if stat.st_size < offset:  # truncated: start over
                offset = 0
            if stat.st_size > offset:
                chunk = await asyncio.to_thread(_read_from, path, offset)
                offset += len(chunk.encode())
                for line in chunk.splitlines():
                    if line.strip():
                        await ctx.report(OutputObservation(native_id=session.native_id, text=line))
            elif time.time() - stat.st_mtime > self._idle_timeout:
                await ctx.report(
                    StateObservation(
                        native_id=session.native_id,
                        state=SessionState.COMPLETED,
                        reason="idle",
                    )
                )
                return  # a normal return means "this session is over"
            await asyncio.sleep(self._poll_interval)


def _read_from(path: Path, offset: int) -> str:
    with path.open("rb") as fh:
        fh.seek(offset)
        return fh.read().decode("utf-8", errors="replace")
