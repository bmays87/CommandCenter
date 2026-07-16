"""Claude Code adapter: observe via transcript watching, control via the SDK.

Sessions are the ``*.jsonl`` transcripts under Claude Code's projects
directory. Discovery scans the directory; ``watch`` tails one transcript,
feeding complete lines through the versioned parser and reporting the
resulting observations. Per-session byte offsets persist in the adapter's
data directory so a server restart does not replay history into the event
log twice.

Control (launch/terminate/respond/send_prompt) uses the Claude Agent SDK via
:mod:`.launcher`. SDK-launched sessions write the same transcripts, so they
are *observed* exactly like manual ones (ADR-0008); control only applies to
sessions this server launched. Permission requests from launched sessions
surface as ``InteractionObservation``s and are answered through ``respond()``.

All file IO happens in threads (async discipline: nothing blocks the loop).
"""

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prodeo.adapters.context import AdapterContext
from prodeo.adapters.interface import (
    AdapterCapabilities,
    AdapterMetadata,
    InteractionRef,
    LaunchSpec,
    ObserveOnlyAdapter,
    SessionRef,
)
from prodeo.adapters.observations import (
    InteractionObservation,
    SessionObservation,
    StateObservation,
)
from prodeo.errors import CapabilityNotSupportedError
from prodeo.mediation.model import Answer, InteractionKind
from prodeo.sessions.model import SessionDescriptor
from prodeo.sessions.state import SessionState
from prodeo_adapter_claude_code.launcher import ClientFactory, SdkLauncher, sdk_available
from prodeo_adapter_claude_code.parser import TranscriptParser

VERSION = "0.3.0"

_PEEK_BYTES = 64 * 1024  # how much of a transcript discovery reads for metadata
_INTERACTION_BODY_CHARS = 4000  # tool input shown to the human, capped


@dataclass
class _TranscriptFile:
    path: Path
    native_id: str
    mtime: float
    size: int


class ClaudeCodeAdapter(ObserveOnlyAdapter):
    """Claude Code adapter: transcript observation + SDK control."""

    def __init__(self, client_factory: ClientFactory | None = None) -> None:
        self.metadata = AdapterMetadata(name="claude-code", version=VERSION)
        control = client_factory is not None or sdk_available()
        self.capabilities = AdapterCapabilities(
            observe=True,
            historical_sessions=True,
            launch=control,
            terminate=control,
            respond_to_permissions=control,
            send_prompts=control,
        )
        self._client_factory = client_factory
        self._launcher: SdkLauncher | None = None
        self._owned: set[str] = set()
        self._permission_timeout_s: float | None = None
        self._transcript_wait_s = 30.0
        self._ctx: AdapterContext | None = None
        self._projects_dir = Path.home() / ".claude" / "projects"
        self._idle_timeout = 1800.0
        self._poll_interval = 1.0
        self._max_replay_bytes = 512 * 1024
        self._offsets: dict[str, int] = {}
        self._paths: dict[str, Path] = {}
        self._peek_cache: dict[str, tuple[float, SessionDescriptor]] = {}
        self._stopped = False

    # ----------------------------------------------------------- lifecycle

    async def start(self, ctx: AdapterContext) -> None:
        self._ctx = ctx
        cfg = ctx.config
        if raw_dir := cfg.get("projects_dir"):
            self._projects_dir = Path(str(raw_dir))
        self._idle_timeout = float(cfg.get("idle_timeout_s", self._idle_timeout))
        self._poll_interval = float(cfg.get("poll_interval_s", self._poll_interval))
        self._max_replay_bytes = int(cfg.get("max_replay_bytes", self._max_replay_bytes))
        raw_timeout = cfg.get("permission_timeout_s")
        self._permission_timeout_s = float(raw_timeout) if raw_timeout is not None else None
        if not bool(cfg.get("control_enabled", True)):
            self.capabilities = AdapterCapabilities(observe=True, historical_sessions=True)
        if self.capabilities.launch:
            self._launcher = SdkLauncher(
                on_interaction=self._on_sdk_interaction,
                on_failed=self._on_sdk_failed,
                client_factory=self._client_factory,
            )
        self._stopped = False
        self._offsets = await asyncio.to_thread(self._load_offsets)
        ctx.logger.info(
            "claude_code.started",
            projects_dir=str(self._projects_dir),
            control=self.capabilities.launch,
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._launcher is not None:
            await self._launcher.close()

    # ------------------------------------------------------------- control

    async def launch(self, spec: LaunchSpec) -> SessionRef:
        launcher = self._require_launcher("launch")
        native_id = await launcher.launch(spec)
        self._owned.add(native_id)
        return SessionRef(adapter=self.metadata.name, native_id=native_id, session_id="")

    async def terminate(self, session: SessionRef) -> None:
        launcher = self._require_launcher("terminate")
        self._require_owned(session.native_id)
        await launcher.terminate(session.native_id)
        if self._ctx is not None:
            await self._ctx.report(
                StateObservation(
                    native_id=session.native_id, state=SessionState.STOPPED, reason="terminated"
                )
            )

    async def respond(self, interaction: InteractionRef, answer: Answer) -> None:
        launcher = self._require_launcher("respond")
        await launcher.respond(interaction.session_native_id, interaction.native_id, answer)

    async def send_prompt(self, session: SessionRef, prompt: str) -> None:
        launcher = self._require_launcher("send_prompt")
        self._require_owned(session.native_id)
        await launcher.send_prompt(session.native_id, prompt)

    def _require_launcher(self, capability: str) -> SdkLauncher:
        if self._launcher is None:
            raise CapabilityNotSupportedError(capability)
        return self._launcher

    def _require_owned(self, native_id: str) -> None:
        if native_id not in self._owned:
            raise RuntimeError(
                f"session {native_id} was not launched by this server (observe-only)"
            )

    async def _on_sdk_interaction(
        self, native_id: str, interaction_native_id: str, tool_name: str, input_data: dict[str, Any]
    ) -> None:
        assert self._ctx is not None
        body = json.dumps(input_data, indent=2, default=str)[:_INTERACTION_BODY_CHARS]
        await self._ctx.report(
            InteractionObservation(
                native_id=native_id,
                interaction_native_id=interaction_native_id,
                kind=InteractionKind.PERMISSION,
                title=f"Allow {tool_name}?",
                body=body,
                timeout_s=self._permission_timeout_s,
            )
        )

    async def _on_sdk_failed(self, native_id: str, reason: str) -> None:
        assert self._ctx is not None
        await self._ctx.report(
            StateObservation(native_id=native_id, state=SessionState.FAILED, reason=reason)
        )

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

    def _scan(self) -> list[_TranscriptFile]:
        if not self._projects_dir.is_dir():
            return []
        found: list[_TranscriptFile] = []
        for path in self._projects_dir.glob("*/*.jsonl"):
            with contextlib.suppress(OSError):
                stat = path.stat()
                found.append(
                    _TranscriptFile(
                        path=path, native_id=path.stem, mtime=stat.st_mtime, size=stat.st_size
                    )
                )
        return found

    def _describe(self, f: _TranscriptFile) -> SessionDescriptor:
        parser = TranscriptParser(native_id=f.native_id)
        with contextlib.suppress(OSError):
            with f.path.open("rb") as fh:
                head = fh.read(_PEEK_BYTES)
            for line in head.decode("utf-8", errors="replace").splitlines():
                parser.feed_line(line)
        return self._descriptor(f.native_id, parser, mtime=f.mtime, path=f.path)

    def _descriptor(
        self, native_id: str, parser: TranscriptParser, *, mtime: float, path: Path
    ) -> SessionDescriptor:
        meta = parser.meta
        idle = time.time() - mtime
        state = SessionState.RUNNING if idle < self._idle_timeout else SessionState.COMPLETED
        return SessionDescriptor(
            native_id=native_id,
            title=meta.title,
            project=meta.project or path.parent.name,
            model=meta.model or None,
            state=state,
            last_activity_at=datetime.fromtimestamp(mtime, tz=UTC),
            metadata={
                k: v
                for k, v in {
                    "git_branch": meta.git_branch,
                    "agent_version": meta.agent_version,
                    "transcript": str(path),
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
        if path is None and native_id in self._owned:
            # SDK-launched sessions create their transcript slightly after
            # launch() returns; wait for it instead of misreporting STOPPED.
            deadline = time.time() + self._transcript_wait_s
            while path is None and time.time() < deadline and not self._stopped:
                await asyncio.sleep(self._poll_interval)
                await self.discover_sessions()
                path = self._paths.get(native_id)
        if path is None:
            await ctx.report(
                StateObservation(
                    native_id=native_id, state=SessionState.STOPPED, reason="transcript_missing"
                )
            )
            return

        parser = TranscriptParser(native_id=native_id)
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
                        native_id=native_id, state=SessionState.STOPPED, reason="transcript_removed"
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
                            descriptor=self._descriptor(
                                native_id, parser, mtime=stat.st_mtime, path=path
                            )
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
        """First-ever watch: replay history, but cap it for huge transcripts."""
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
