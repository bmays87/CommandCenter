"""CommandHandlers: intents against the cache, commands through the client."""

import pytest
from mjolnir_fakes import FakeServerClient, make_interaction, make_session, started_cache

from prodeo.sessions import SessionState
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.handlers import CommandHandlers
from prodeo_mjolnir.intents import (
    ApproveIntent,
    DenyIntent,
    OvernightIntent,
    PendingIntent,
    StatusIntent,
    StopIntent,
    UnknownIntent,
)
from prodeo_mjolnir.packs import NEUTRAL


async def _handlers(client: FakeServerClient) -> CommandHandlers:
    cache = await started_cache(client)
    return CommandHandlers(cache, client.as_client(), ResponseComposer(NEUTRAL, honorific="sir"))


@pytest.mark.asyncio
async def test_status_reports_active_and_pending() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", title="nightly-refactor", active_ago_s=30),
        make_session("s2", project="/repos/api-tests", state=SessionState.WAITING_ON_USER),
        make_session("s3", project="/old", state=SessionState.COMPLETED),
    ]
    client.interactions = [make_interaction("i1", "s2", title="Run the migration?")]
    handlers = await _handlers(client)

    text = await handlers.handle(StatusIntent())
    assert text == (
        "2 sessions active, sir: nightly-refactor and api-tests. "
        "1 interaction awaiting your answer."
    )

    client.sessions = []
    client.interactions = []
    empty = await _handlers(FakeServerClient())
    assert await empty.handle(StatusIntent()) == "No sessions are active, sir."


@pytest.mark.asyncio
async def test_overnight_briefing_covers_all_three_agents() -> None:
    """The vision.md morning scenario: one finished, one blocked, one failed."""
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", title="nightly-refactor", state=SessionState.COMPLETED),
        make_session("s2", project="/repos/api-tests", state=SessionState.FAILED),
        make_session("s3", project="/repos/db", state=SessionState.WAITING_ON_USER),
    ]
    client.interactions = [make_interaction("i1", "s3", title="May I run the database migration?")]
    handlers = await _handlers(client)

    text = await handlers.handle(OvernightIntent())
    assert "3 agent sessions ran while you were away, sir." in text
    assert "nightly-refactor finished." in text
    assert "api-tests failed." in text
    assert "db is waiting on you: May I run the database migration?" in text


@pytest.mark.asyncio
async def test_overnight_quiet() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("old", state=SessionState.COMPLETED, active_ago_s=3600 * 24)]
    handlers = await _handlers(client)
    assert await handlers.handle(OvernightIntent()) == (
        "All quiet, sir. No agent activity in the last 12 hours."
    )


@pytest.mark.asyncio
async def test_pending_none_one_many() -> None:
    client = FakeServerClient()
    handlers = await _handlers(client)
    assert await handlers.handle(PendingIntent()) == "Nothing is waiting on you, sir."

    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Run it?")]
    handlers = await _handlers(client)
    assert await handlers.handle(PendingIntent()) == (
        "One thing needs you, sir. claude-code on db asks: Run it?"
    )


@pytest.mark.asyncio
async def test_approve_single_pending_needs_no_target() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Run the migration?")]
    handlers = await _handlers(client)

    assert await handlers.handle(ApproveIntent()) == "Approved, sir."
    assert client.answered == [("i1", "allow")]


@pytest.mark.asyncio
async def test_approve_by_target_and_ambiguity() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/db-migration"),
        make_session("s2", project="/repos/api"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the database migration?"),
        make_interaction("i2", "s2", title="Delete fixtures?"),
    ]
    handlers = await _handlers(client)

    # several pending + no target: the handler says what's first instead of guessing
    text = await handlers.handle(ApproveIntent())
    assert text.startswith("2 things need you, sir.")
    assert client.answered == []

    assert await handlers.handle(ApproveIntent(target="database migration")) == "Approved, sir."
    assert client.answered == [("i1", "allow")]

    assert await handlers.handle(DenyIntent(target="fixtures")) == "Denied, sir."
    assert client.answered == [("i1", "allow"), ("i2", "deny")]

    assert "couldn't find" in await handlers.handle(ApproveIntent(target="the moon lander"))


@pytest.mark.asyncio
async def test_approve_lost_race_is_reported_gracefully() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Run it?")]
    client.already_resolved.add("i1")
    handlers = await _handlers(client)
    assert await handlers.handle(ApproveIntent()) == "That was already answered elsewhere, sir."


@pytest.mark.asyncio
async def test_stop_by_name_and_ambiguity() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", title="nightly-refactor"),
        make_session("s2", project="/repos/api-tests"),
    ]
    handlers = await _handlers(client)

    assert await handlers.handle(StopIntent(target="nightly refactor")) == (
        "nightly-refactor has been stopped, sir."
    )
    assert client.terminated == ["s1"]

    ambiguous = await handlers.handle(StopIntent())  # two active, no target
    assert "2 sessions match" in ambiguous
    assert "couldn't find" in await handlers.handle(StopIntent(target="ghost"))


@pytest.mark.asyncio
async def test_unknown_echoes_the_transcript() -> None:
    handlers = await _handlers(FakeServerClient())
    text = await handlers.handle(UnknownIntent(text="make me a sandwich"))
    assert text == "Sorry, sir, I didn't understand: make me a sandwich."
