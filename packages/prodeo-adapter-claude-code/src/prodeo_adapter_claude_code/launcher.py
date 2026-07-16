"""SDK-backed control of Claude Code sessions (launch/terminate/respond).

All contact with ``claude-agent-sdk`` is isolated here, behind an injectable
``client_factory`` so tests never touch the real SDK. Launched sessions write
the same JSONL transcripts as interactive ones, so *observation* stays with
the transcript watcher (ADR-0008); the SDK stream is used only for control:
learning the session id, mediating permission callbacks, and surfacing
failures the transcript cannot show.

The permission bridge: the SDK's ``can_use_tool`` callback may await
indefinitely, so it parks on an ``asyncio.Future[Answer]`` that
``respond()`` resolves when a human answers through the Mediation Service.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from ulid import ULID

from prodeo.adapters.interface import LaunchSpec
from prodeo.mediation.model import Answer

#: (tool_name, tool_input) -> the human's answer. Provided by the launcher to
#: the client factory, which adapts it to the SDK's ``can_use_tool`` types.
DecideFn = Callable[[str, dict[str, Any]], Awaitable[Answer]]

#: (native_id, interaction_native_id, tool_name, tool_input) - a permission
#: request is waiting on a human.
OnInteraction = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]

#: (native_id, reason) - the SDK connection failed in a way the transcript
#: watcher cannot observe.
OnFailed = Callable[[str, str], Awaitable[None]]

#: How long to wait for the SDK to reveal the session id after launch.
INIT_TIMEOUT_S = 30.0


class SdkClient(Protocol):
    """The slice of ``ClaudeSDKClient`` the launcher uses (fakeable in tests)."""

    async def connect(self) -> None: ...

    async def query(self, prompt: str, session_id: str = "default") -> None: ...

    def receive_messages(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


ClientFactory = Callable[[LaunchSpec, DecideFn], SdkClient]


def sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def default_client_factory(spec: LaunchSpec, decide: DecideFn) -> SdkClient:
    """Build a real ``ClaudeSDKClient``; the only place SDK types appear."""
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
    )

    async def can_use_tool(
        tool_name: str, input_data: dict[str, Any], _context: Any
    ) -> "PermissionResultAllow | PermissionResultDeny":
        answer = await decide(tool_name, input_data)
        if answer.decision == "allow":
            return PermissionResultAllow(updated_input=answer.updated_input)
        return PermissionResultDeny(message=answer.text or "denied via Command Center")

    kwargs: dict[str, Any] = dict(spec.options)
    if spec.project:
        kwargs["cwd"] = spec.project
    if spec.model:
        kwargs["model"] = spec.model
    if spec.permission_mode:
        kwargs["permission_mode"] = spec.permission_mode
    options = ClaudeAgentOptions(can_use_tool=can_use_tool, **kwargs)
    return ClaudeSDKClient(options=options)


class _SdkSession:
    """One launched session: its client, drain task, and pending permissions."""

    def __init__(self) -> None:
        self.client: SdkClient | None = None
        self.native_id: str = ""
        self.sid_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending: dict[str, asyncio.Future[Answer]] = {}
        self.drain: asyncio.Task[None] | None = None
        self.closed = False


def _session_id_of(message: Any) -> str | None:
    """Extract a session id from an SDK message, whatever its shape."""
    sid = getattr(message, "session_id", None)
    if isinstance(sid, str) and sid:
        return sid
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        candidate = data.get("session_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


class SdkLauncher:
    """Owns every SDK-launched session for one adapter instance."""

    def __init__(
        self,
        *,
        on_interaction: OnInteraction,
        on_failed: OnFailed,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._on_interaction = on_interaction
        self._on_failed = on_failed
        self._client_factory = client_factory or default_client_factory
        self._sessions: dict[str, _SdkSession] = {}

    # -------------------------------------------------------------- control

    async def launch(self, spec: LaunchSpec) -> str:
        """Start a session; returns its native id (the Claude session UUID)."""
        session = _SdkSession()

        async def decide(tool_name: str, input_data: dict[str, Any]) -> Answer:
            return await self._decide(session, tool_name, input_data)

        client = self._client_factory(spec, decide)
        session.client = client
        await client.connect()
        await client.query(spec.prompt)
        session.drain = asyncio.create_task(self._drain(session), name="claude-code-sdk-drain")
        try:
            native_id = await asyncio.wait_for(session.sid_future, timeout=INIT_TIMEOUT_S)
        except Exception:
            await self._shutdown(session)
            raise
        self._sessions[native_id] = session
        return native_id

    async def terminate(self, native_id: str) -> None:
        session = self._require(native_id)
        await self._shutdown(session)
        self._sessions.pop(native_id, None)

    async def respond(self, native_id: str, interaction_native_id: str, answer: Answer) -> None:
        session = self._require(native_id)
        future = session.pending.get(interaction_native_id)
        if future is None or future.done():
            raise RuntimeError(f"no pending interaction {interaction_native_id} in {native_id}")
        future.set_result(answer)

    async def send_prompt(self, native_id: str, prompt: str) -> None:
        session = self._require(native_id)
        assert session.client is not None
        await session.client.query(prompt)

    def owns(self, native_id: str) -> bool:
        return native_id in self._sessions

    async def close(self) -> None:
        for session in list(self._sessions.values()):
            await self._shutdown(session)
        self._sessions.clear()

    # ------------------------------------------------------------- internal

    def _require(self, native_id: str) -> _SdkSession:
        session = self._sessions.get(native_id)
        if session is None:
            raise RuntimeError(f"session {native_id} is not controlled by this server")
        return session

    async def _decide(
        self, session: _SdkSession, tool_name: str, input_data: dict[str, Any]
    ) -> Answer:
        interaction_native_id = str(ULID())
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        session.pending[interaction_native_id] = future
        try:
            await self._on_interaction(
                session.native_id, interaction_native_id, tool_name, input_data
            )
            return await future  # indefinite: the SDK pauses until we return
        finally:
            session.pending.pop(interaction_native_id, None)

    async def _drain(self, session: _SdkSession) -> None:
        """Consume the SDK stream: session id + failure detection only (ADR-0008)."""
        assert session.client is not None
        try:
            async for message in session.client.receive_messages():
                sid = _session_id_of(message)
                if sid and not session.native_id:
                    session.native_id = sid
                    if not session.sid_future.done():
                        session.sid_future.set_result(sid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not session.sid_future.done():
                session.sid_future.set_exception(exc)
                return
            if not session.closed and session.native_id:
                await self._on_failed(session.native_id, str(exc))

    async def _shutdown(self, session: _SdkSession) -> None:
        session.closed = True
        if session.client is not None:
            with contextlib.suppress(Exception):
                await session.client.interrupt()
            with contextlib.suppress(Exception):
                await session.client.disconnect()
        if session.drain is not None and not session.drain.done():
            session.drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.drain
        for future in session.pending.values():
            if not future.done():
                future.cancel()
        session.pending.clear()
