"""The voice client's view of the server: REST commands + WebSocket stream.

Mjölnir talks to the core over exactly the same API as the dashboard - it is
a client, not a subsystem. Command latency matters (spoken confirmations),
so state *queries* go to the event-stream-fed :class:`~prodeo_mjolnir.cache.LocalCache`
instead; this module only performs commands, snapshots, and event reporting.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

import httpx
import structlog
import websockets
from ulid import ULID

from prodeo.events import Event
from prodeo.mediation import Interaction
from prodeo.sessions import Session
from prodeo_mjolnir.errors import AlreadyResolvedError, ServerRequestError

_log = structlog.get_logger(__name__)


class ServerClient:
    """Thin async wrapper over the server's REST + WebSocket API."""

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str = "",
        client_id: str = "mjolnir",
        node: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = api_token
        self._client_id = client_id
        self._node = node
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
        self._http = httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=10.0, transport=transport
        )

    @property
    def client_id(self) -> str:
        return self._client_id

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ snapshots

    async def list_sessions(self) -> list[Session]:
        data = await self._get_json("/api/sessions")
        return [Session.model_validate(s) for s in data["sessions"]]

    async def list_pending_interactions(self) -> list[Interaction]:
        data = await self._get_json("/api/interactions", params={"status": "pending"})
        return [Interaction.model_validate(i) for i in data["interactions"]]

    async def events_since(
        self, since: datetime, *, type_pattern: str = "*", limit: int = 500
    ) -> list[Event]:
        """Events at or after ``since`` (ULIDs sort by time, so a cursor built
        from the timestamp pages the log without a timestamp filter)."""
        cursor = str(ULID.from_timestamp(since.timestamp()))
        out: list[Event] = []
        while True:
            data = await self._get_json(
                "/api/events",
                params={"after": cursor, "type": type_pattern, "limit": limit},
            )
            batch = [Event.model_validate(e) for e in data["events"]]
            out.extend(batch)
            if len(batch) < limit:
                return out
            cursor = batch[-1].id

    # ------------------------------------------------------------- commands

    async def answer(
        self,
        interaction_id: str,
        *,
        decision: Literal["allow", "deny"] | None = None,
        text: str = "",
    ) -> Interaction:
        response = await self._http.post(
            f"/api/interactions/{interaction_id}/answer",
            json={"decision": decision, "text": text},
        )
        if response.status_code == 409:
            raise AlreadyResolvedError(interaction_id)
        self._raise_for_status(response)
        return Interaction.model_validate(response.json())

    async def terminate(self, session_id: str) -> None:
        self._raise_for_status(await self._http.post(f"/api/sessions/{session_id}/terminate"))

    # ------------------------------------------------------------ reporting

    async def post_voice_event(
        self,
        type_: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Report one ``voice.*`` event into the server's log (contained:
        a reporting failure must never break the voice loop)."""
        try:
            response = await self._http.post(
                "/api/voice/events",
                json={
                    "type": type_,
                    "client_id": self._client_id,
                    "node": self._node,
                    "payload": payload or {},
                    "session_id": session_id,
                    "correlation_id": correlation_id,
                },
            )
            self._raise_for_status(response)
        except Exception as exc:
            _log.warning("client.voice_event_failed", type=type_, error=str(exc))

    async def report_presence(self, *, attentive: bool, ttl_s: float) -> None:
        """Heartbeat (contained: a missed heartbeat only means expiry)."""
        try:
            response = await self._http.put(
                f"/api/presence/{self._client_id}",
                json={
                    "kind": "voice",
                    "attentive": attentive,
                    "node": self._node,
                    "ttl_s": ttl_s,
                },
            )
            self._raise_for_status(response)
        except Exception as exc:
            _log.warning("client.presence_failed", error=str(exc))

    async def forget_presence(self) -> None:
        try:
            await self._http.delete(f"/api/presence/{self._client_id}")
        except Exception as exc:
            _log.warning("client.presence_forget_failed", error=str(exc))

    # --------------------------------------------------------- event stream

    async def stream_events(self, types: list[str]) -> AsyncIterator[Event]:
        """Live events matching ``types``, reconnecting with a ULID cursor.

        Yields forever (reconnect with backoff); cancel the consuming task to
        stop.
        """
        cursor: str | None = None
        backoff = 1.0
        while True:
            url = self._ws_url(types, cursor)
            try:
                async with websockets.connect(url) as ws:
                    backoff = 1.0
                    async for frame in ws:
                        text = frame if isinstance(frame, str) else frame.decode()
                        event = Event.model_validate_json(text)
                        cursor = event.id
                        yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("client.stream_reconnecting", error=str(exc), backoff_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _ws_url(self, types: list[str], cursor: str | None) -> str:
        scheme = "wss" if self._base_url.startswith("https") else "ws"
        host = self._base_url.split("://", 1)[1]
        params = [f"types={','.join(types)}"]
        if self._token:
            params.append(f"token={self._token}")
        if cursor:
            params.append(f"after={cursor}")
        return f"{scheme}://{host}/api/ws/events?{'&'.join(params)}"

    # -------------------------------------------------------------- helpers

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._http.get(path, params=params)
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ServerRequestError(f"{response.request.url.path}: {response.status_code} {detail}")
