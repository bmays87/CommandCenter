"""Mediation Service: owns the interaction lifecycle.

Publishes ``interaction.*`` facts, tracks timeouts, and accepts **exactly one
resolution** per interaction - the first answer wins; later answers are
rejected with :class:`InteractionAlreadyResolvedError`. Resolution is atomic
because status flips synchronously (no ``await`` between the pending check and
the flip) on the single event loop.

Answers are routed back to the blocked agent through a per-interaction
``deliver`` callback supplied at :meth:`MediationService.open` (the Adapter
Manager passes a closure over the owning adapter). Mediation therefore knows
nothing about adapters. Delivery callbacks do not survive a restart, so
:meth:`rebuild` cancels any interaction that was still pending in the log
rather than resurrecting an unanswerable zombie (ADR-0007).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog
from ulid import ULID

from prodeo.bus.interface import EventBus
from prodeo.errors import InteractionAlreadyResolvedError, UnknownInteractionError
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.mediation.model import (
    Answer,
    Interaction,
    InteractionKind,
    InteractionRequest,
    InteractionStatus,
)
from prodeo.persistence.interface import EventQuery, EventStore

_log = structlog.get_logger(__name__)

_SOURCE = "mediation"

DeliverFn = Callable[[Interaction, Answer], Awaitable[None]]


class MediationService:
    """In-memory interaction catalogue, event-sourced, single event loop."""

    def __init__(
        self,
        bus: EventBus,
        *,
        node: str = "local",
        default_timeout_s: float | None = None,
    ) -> None:
        self._bus = bus
        self._node = node
        self._default_timeout_s = default_timeout_s
        self._by_id: dict[str, Interaction] = {}
        #: pending interactions only, keyed by adapter-native identity
        self._pending_by_native: dict[tuple[str, str], str] = {}
        self._deliver: dict[str, DeliverFn] = {}
        self._timeouts: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------- queries

    def list_interactions(
        self,
        *,
        status: InteractionStatus | None = None,
        session_id: str | None = None,
    ) -> list[Interaction]:
        """Interactions, oldest first (ULIDs sort chronologically)."""
        items = sorted(self._by_id.values(), key=lambda i: i.id)
        if status is not None:
            items = [i for i in items if i.status == status]
        if session_id is not None:
            items = [i for i in items if i.session_id == session_id]
        return items

    def get(self, interaction_id: str) -> Interaction | None:
        return self._by_id.get(interaction_id)

    def pending_count(self) -> int:
        return len(self._pending_by_native)

    # ------------------------------------------------------------ commands

    async def open(self, request: InteractionRequest, deliver: DeliverFn) -> Interaction:
        """Open an interaction, publishing ``interaction.requested``.

        Idempotent per adapter-native identity: re-opening one that is still
        pending refreshes the deliver callback (the adapter may have restarted
        its watch) and returns the existing record without a duplicate event.
        """
        key = (request.adapter, request.native_id)
        existing_id = self._pending_by_native.get(key)
        if existing_id is not None:
            self._deliver[existing_id] = deliver
            return self._by_id[existing_id]

        now = datetime.now(UTC)
        timeout_s = request.timeout_s if request.timeout_s is not None else self._default_timeout_s
        interaction = Interaction(
            id=str(ULID()),
            session_id=request.session_id,
            adapter=request.adapter,
            native_id=request.native_id,
            kind=request.kind,
            title=request.title,
            body=request.body,
            options=list(request.options),
            requested_at=now,
            timeout_at=now + timedelta(seconds=timeout_s) if timeout_s is not None else None,
        )
        self._by_id[interaction.id] = interaction
        self._pending_by_native[key] = interaction.id
        self._deliver[interaction.id] = deliver
        if timeout_s is not None:
            self._timeouts[interaction.id] = asyncio.create_task(
                self._timeout(interaction.id, timeout_s),
                name=f"interaction-timeout:{interaction.id}",
            )
        await self._bus.publish(
            new_event(
                ev.INTERACTION_REQUESTED,
                node=self._node,
                source=_SOURCE,
                session_id=interaction.session_id,
                payload={"interaction": interaction.model_dump(mode="json")},
            )
        )
        _log.info(
            "mediation.opened",
            interaction_id=interaction.id,
            session_id=interaction.session_id,
            kind=interaction.kind,
        )
        return interaction

    async def answer(
        self, interaction_id: str, answer: Answer, *, answered_by: str = "api"
    ) -> Interaction:
        """Resolve an interaction with a human answer (exactly once).

        The answer is the durable fact: ``interaction.answered`` publishes
        before delivery is attempted, and a delivery failure is logged rather
        than rolled back (the deliver closure additionally reports it as an
        ``adapter.error``).
        """
        interaction = self._by_id.get(interaction_id)
        if interaction is None:
            raise UnknownInteractionError(interaction_id)
        if interaction.status is not InteractionStatus.PENDING:
            raise InteractionAlreadyResolvedError(interaction_id, interaction.status)

        interaction.status = InteractionStatus.ANSWERED
        interaction.answer = answer
        interaction.answered_by = answered_by
        interaction.answered_at = datetime.now(UTC)
        deliver = self._resolve_bookkeeping(interaction)

        await self._bus.publish(
            new_event(
                ev.INTERACTION_ANSWERED,
                node=self._node,
                source=_SOURCE,
                session_id=interaction.session_id,
                payload={
                    "interaction_id": interaction.id,
                    "answer": answer.model_dump(mode="json"),
                    "answered_by": answered_by,
                },
            )
        )
        if deliver is not None:
            try:
                await deliver(interaction, answer)
            except Exception:
                _log.exception("mediation.deliver_failed", interaction_id=interaction.id)
        return interaction

    async def cancel_native(self, adapter: str, native_id: str, *, reason: str) -> None:
        """Cancel a pending interaction the adapter has withdrawn (no-op if gone)."""
        interaction_id = self._pending_by_native.get((adapter, native_id))
        if interaction_id is not None:
            await self._cancel(self._by_id[interaction_id], reason=reason)

    # ------------------------------------------------------------- rebuild

    async def rebuild(self, store: EventStore) -> None:
        """Fold the persisted ``interaction.*`` log; cancel orphaned pendings.

        Must run after the event recorder is started so the orphan
        cancellations published here reach the log.
        """
        cursor: str | None = None
        count = 0
        while True:
            batch = await store.query(
                EventQuery(after_id=cursor, type_pattern="interaction.*", limit=500)
            )
            if not batch:
                break
            for event in batch:
                self._apply(event)
                count += 1
            cursor = batch[-1].id
        orphans = [i for i in self._by_id.values() if i.status is InteractionStatus.PENDING]
        for interaction in orphans:
            await self._cancel(interaction, reason="orphaned_by_restart")
        _log.info(
            "mediation.rebuilt",
            events=count,
            interactions=len(self._by_id),
            orphans_cancelled=len(orphans),
        )

    def _apply(self, event: Event) -> None:
        payload = event.payload
        if event.type == ev.INTERACTION_REQUESTED:
            interaction = Interaction.model_validate(payload["interaction"])
            self._by_id[interaction.id] = interaction
            if interaction.status is InteractionStatus.PENDING:
                self._pending_by_native[(interaction.adapter, interaction.native_id)] = (
                    interaction.id
                )
            return
        interaction_or_none = self._by_id.get(payload.get("interaction_id", ""))
        if interaction_or_none is None:
            _log.warning("mediation.orphan_event", event_id=event.id, type=event.type)
            return
        interaction = interaction_or_none
        self._pending_by_native.pop((interaction.adapter, interaction.native_id), None)
        if event.type == ev.INTERACTION_ANSWERED:
            interaction.status = InteractionStatus.ANSWERED
            interaction.answer = Answer.model_validate(payload["answer"])
            interaction.answered_by = str(payload.get("answered_by", ""))
            interaction.answered_at = event.timestamp
        elif event.type == ev.INTERACTION_TIMED_OUT:
            interaction.status = InteractionStatus.TIMED_OUT
        elif event.type == ev.INTERACTION_CANCELLED:
            interaction.status = InteractionStatus.CANCELLED

    # ------------------------------------------------------------ shutdown

    async def close(self) -> None:
        """Cancel timeout tasks; pending interactions stay pending in the log
        and are cancelled by :meth:`rebuild` on the next boot."""
        tasks = list(self._timeouts.values())
        self._timeouts.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------ internal

    def _resolve_bookkeeping(self, interaction: Interaction) -> DeliverFn | None:
        """Drop pending-state bookkeeping; returns the deliver callback, if any."""
        self._pending_by_native.pop((interaction.adapter, interaction.native_id), None)
        timeout = self._timeouts.pop(interaction.id, None)
        if timeout is not None and timeout is not asyncio.current_task():
            timeout.cancel()
        return self._deliver.pop(interaction.id, None)

    async def _cancel(self, interaction: Interaction, *, reason: str) -> None:
        interaction.status = InteractionStatus.CANCELLED
        self._resolve_bookkeeping(interaction)
        await self._bus.publish(
            new_event(
                ev.INTERACTION_CANCELLED,
                node=self._node,
                source=_SOURCE,
                session_id=interaction.session_id,
                payload={"interaction_id": interaction.id, "reason": reason},
            )
        )
        _log.info("mediation.cancelled", interaction_id=interaction.id, reason=reason)

    async def _timeout(self, interaction_id: str, timeout_s: float) -> None:
        await asyncio.sleep(timeout_s)
        interaction = self._by_id.get(interaction_id)
        if interaction is None or interaction.status is not InteractionStatus.PENDING:
            return
        interaction.status = InteractionStatus.TIMED_OUT
        deliver = self._resolve_bookkeeping(interaction)
        await self._bus.publish(
            new_event(
                ev.INTERACTION_TIMED_OUT,
                node=self._node,
                source=_SOURCE,
                session_id=interaction.session_id,
                payload={"interaction_id": interaction.id},
            )
        )
        _log.info("mediation.timed_out", interaction_id=interaction.id)
        # A permission left unanswered auto-denies so the blocked agent can
        # move on safely; an expired question simply stops being answerable.
        if interaction.kind is InteractionKind.PERMISSION and deliver is not None:
            try:
                await deliver(interaction, Answer(decision="deny", text="timed out"))
            except Exception:
                _log.exception("mediation.deliver_failed", interaction_id=interaction.id)
