"""Mediation lifecycle: exactly-once resolution, timeouts, rebuild semantics."""

import asyncio
from pathlib import Path

import pytest

from prodeo.bus import InProcessEventBus
from prodeo.errors import InteractionAlreadyResolvedError, UnknownInteractionError
from prodeo.events import Event
from prodeo.events import types as ev
from prodeo.mediation import (
    Answer,
    Interaction,
    InteractionKind,
    InteractionRequest,
    InteractionStatus,
    MediationService,
)
from prodeo.persistence import SqliteEventStore


async def _drain(sub: object) -> list[Event]:
    out: list[Event] = []
    while True:
        try:
            async with asyncio.timeout(0.05):
                async for event in sub:  # type: ignore[attr-defined]
                    out.append(event)
        except TimeoutError:
            return out


class Delivered:
    """Records answers delivered to the (fake) adapter side."""

    def __init__(self) -> None:
        self.answers: list[Answer] = []

    async def __call__(self, interaction: Interaction, answer: Answer) -> None:
        self.answers.append(answer)


def _request(
    native_id: str = "tool-1",
    kind: InteractionKind = InteractionKind.PERMISSION,
    timeout_s: float | None = None,
) -> InteractionRequest:
    return InteractionRequest(
        session_id="sess-1",
        adapter="claude-code",
        native_id=native_id,
        kind=kind,
        title="Run `rm -rf build`?",
        timeout_s=timeout_s,
    )


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.mark.asyncio
async def test_open_publishes_requested_and_lists_pending(bus: InProcessEventBus) -> None:
    sub = bus.subscribe("interaction.*", name="probe")
    service = MediationService(bus)

    interaction = await service.open(_request(), Delivered())

    assert interaction.status is InteractionStatus.PENDING
    assert service.get(interaction.id) is interaction
    assert service.pending_count() == 1
    assert service.list_interactions(status=InteractionStatus.PENDING) == [interaction]
    assert service.list_interactions(session_id="other") == []

    events = await _drain(sub)
    assert [e.type for e in events] == [ev.INTERACTION_REQUESTED]
    assert events[0].session_id == "sess-1"
    assert events[0].payload["interaction"]["kind"] == "permission"


@pytest.mark.asyncio
async def test_reopen_while_pending_is_idempotent(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    first_deliver = Delivered()
    second_deliver = Delivered()
    first = await service.open(_request(), first_deliver)
    sub = bus.subscribe("interaction.*", name="probe")

    again = await service.open(_request(), second_deliver)

    assert again is first
    assert await _drain(sub) == []

    await service.answer(first.id, Answer(decision="allow"))
    assert first_deliver.answers == []  # refreshed callback wins
    assert [a.decision for a in second_deliver.answers] == ["allow"]


@pytest.mark.asyncio
async def test_answer_delivers_and_publishes_fact(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    delivered = Delivered()
    interaction = await service.open(_request(), delivered)
    sub = bus.subscribe("interaction.*", name="probe")

    answered = await service.answer(
        interaction.id, Answer(decision="allow"), answered_by="dashboard"
    )

    assert answered.status is InteractionStatus.ANSWERED
    assert answered.answered_by == "dashboard"
    assert answered.answered_at is not None
    assert [a.decision for a in delivered.answers] == ["allow"]
    assert service.pending_count() == 0

    events = await _drain(sub)
    assert [e.type for e in events] == [ev.INTERACTION_ANSWERED]
    assert events[0].payload["answer"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_second_answer_rejected_and_not_delivered(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    delivered = Delivered()
    interaction = await service.open(_request(), delivered)
    await service.answer(interaction.id, Answer(decision="allow"))

    with pytest.raises(InteractionAlreadyResolvedError):
        await service.answer(interaction.id, Answer(decision="deny"))

    assert len(delivered.answers) == 1


@pytest.mark.asyncio
async def test_concurrent_answers_exactly_one_wins(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    delivered = Delivered()
    interaction = await service.open(_request(), delivered)

    results = await asyncio.gather(
        service.answer(interaction.id, Answer(decision="allow")),
        service.answer(interaction.id, Answer(decision="deny")),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, InteractionAlreadyResolvedError)]
    assert len(errors) == 1
    assert len(delivered.answers) == 1
    assert delivered.answers[0].decision == "allow"  # first caller won


@pytest.mark.asyncio
async def test_answer_unknown_interaction_raises(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    with pytest.raises(UnknownInteractionError):
        await service.answer("nope", Answer(decision="allow"))


@pytest.mark.asyncio
async def test_timeout_auto_denies_permission(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    delivered = Delivered()
    interaction = await service.open(_request(timeout_s=0.01), delivered)
    sub = bus.subscribe("interaction.*", name="probe")

    await asyncio.sleep(0.05)

    assert interaction.status is InteractionStatus.TIMED_OUT
    assert [a.decision for a in delivered.answers] == ["deny"]
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.INTERACTION_TIMED_OUT]

    with pytest.raises(InteractionAlreadyResolvedError):
        await service.answer(interaction.id, Answer(decision="allow"))


@pytest.mark.asyncio
async def test_timeout_expires_question_without_delivery(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    delivered = Delivered()
    interaction = await service.open(
        _request(kind=InteractionKind.QUESTION, timeout_s=0.01), delivered
    )

    await asyncio.sleep(0.05)

    assert interaction.status is InteractionStatus.TIMED_OUT
    assert delivered.answers == []


@pytest.mark.asyncio
async def test_default_timeout_from_service_config(bus: InProcessEventBus) -> None:
    service = MediationService(bus, default_timeout_s=0.01)
    interaction = await service.open(_request(), Delivered())
    assert interaction.timeout_at is not None

    await asyncio.sleep(0.05)
    assert interaction.status is InteractionStatus.TIMED_OUT


@pytest.mark.asyncio
async def test_answer_cancels_timeout(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    interaction = await service.open(_request(timeout_s=0.02), Delivered())
    await service.answer(interaction.id, Answer(decision="allow"))

    await asyncio.sleep(0.05)
    assert interaction.status is InteractionStatus.ANSWERED  # timeout did not fire


@pytest.mark.asyncio
async def test_cancel_native_withdraws_pending(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    delivered = Delivered()
    interaction = await service.open(_request(), delivered)
    sub = bus.subscribe("interaction.*", name="probe")

    await service.cancel_native("claude-code", "tool-1", reason="answered_in_terminal")
    await service.cancel_native("claude-code", "gone", reason="noop")  # unknown: no-op

    assert interaction.status is InteractionStatus.CANCELLED
    assert delivered.answers == []
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.INTERACTION_CANCELLED]
    assert events[0].payload["reason"] == "answered_in_terminal"


@pytest.mark.asyncio
async def test_deliver_failure_does_not_corrupt_resolution(bus: InProcessEventBus) -> None:
    service = MediationService(bus)

    async def exploding(_interaction: Interaction, _answer: Answer) -> None:
        raise RuntimeError("adapter went away")

    interaction = await service.open(_request(), exploding)
    answered = await service.answer(interaction.id, Answer(decision="allow"))

    assert answered.status is InteractionStatus.ANSWERED
    with pytest.raises(InteractionAlreadyResolvedError):
        await service.answer(interaction.id, Answer(decision="deny"))


@pytest.mark.asyncio
async def test_rebuild_restores_history_and_cancels_orphans(
    bus: InProcessEventBus, tmp_path: Path
) -> None:
    store = SqliteEventStore(tmp_path / "events.db")
    await store.open()
    sub = bus.subscribe("*", name="recorder")

    service = MediationService(bus)
    answered = await service.open(_request(native_id="done"), Delivered())
    await service.answer(answered.id, Answer(decision="allow"), answered_by="dashboard")
    orphan = await service.open(_request(native_id="stuck"), Delivered())
    for event in await _drain(sub):
        await store.append(event)

    rebuilt = MediationService(bus)
    probe = bus.subscribe("interaction.*", name="probe")
    await rebuilt.rebuild(store)

    restored = rebuilt.get(answered.id)
    assert restored is not None
    assert restored.status is InteractionStatus.ANSWERED
    assert restored.answer is not None
    assert restored.answer.decision == "allow"
    assert restored.answered_by == "dashboard"

    restored_orphan = rebuilt.get(orphan.id)
    assert restored_orphan is not None
    assert restored_orphan.status is InteractionStatus.CANCELLED
    assert rebuilt.pending_count() == 0

    cancels = await _drain(probe)
    assert [e.type for e in cancels] == [ev.INTERACTION_CANCELLED]
    assert cancels[0].payload["reason"] == "orphaned_by_restart"
    await store.close()


@pytest.mark.asyncio
async def test_close_cancels_timeout_tasks(bus: InProcessEventBus) -> None:
    service = MediationService(bus)
    interaction = await service.open(_request(timeout_s=30.0), Delivered())

    await service.close()

    assert interaction.status is InteractionStatus.PENDING  # resolved on next boot
