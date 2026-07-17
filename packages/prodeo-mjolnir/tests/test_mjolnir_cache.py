"""LocalCache: snapshot + stream fold (mirrors the server's own folds)."""

import pytest
from mjolnir_fakes import (
    FakeServerClient,
    make_interaction,
    make_session,
    settle,
    started_cache,
)

from prodeo.events import new_event
from prodeo.sessions import SessionState


@pytest.mark.asyncio
async def test_snapshot_then_stream_fold() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/api", state=SessionState.RUNNING)]
    client.interactions = [make_interaction("i1", "s1", title="Run migrations?")]
    cache = await started_cache(client)

    assert [s.id for s in cache.active_sessions()] == ["s1"]
    assert [i.id for i in cache.pending_interactions()] == ["i1"]

    # a new session arrives on the stream, then completes
    s2 = make_session("s2", project="/repos/web")
    client.push(new_event("session.discovered", payload={"session": s2.model_dump(mode="json")}))
    client.push(
        new_event(
            "session.state_changed",
            session_id="s2",
            payload={"from": "discovered", "to": "running"},
        )
    )
    await settle()
    assert {s.id for s in cache.active_sessions()} == {"s1", "s2"}

    client.push(
        new_event(
            "session.state_changed",
            session_id="s2",
            payload={"from": "running", "to": "completed"},
        )
    )
    await settle()
    assert [s.id for s in cache.active_sessions()] == ["s1"]
    completed = cache.session("s2")
    assert completed is not None and completed.state == SessionState.COMPLETED
    assert completed.ended_at is not None

    # interaction resolved elsewhere disappears from pending
    client.push(new_event("interaction.answered", payload={"interaction_id": "i1"}))
    await settle()
    assert cache.pending_interactions() == []

    # unknown session ids and malformed events must not kill the feed
    client.push(new_event("session.state_changed", session_id="ghost", payload={"to": "running"}))
    client.push(new_event("interaction.requested", payload={"interaction": {"bogus": True}}))
    await settle()
    assert cache.session("s1") is not None
    await cache.stop()


@pytest.mark.asyncio
async def test_subscribers_see_the_raw_events() -> None:
    client = FakeServerClient()
    cache = await started_cache(client)
    queue = cache.subscribe()

    event = new_event("interaction.requested", payload={"interaction": {"id": "x"}})
    client.push(event)
    await settle()
    assert (await queue.get()).id == event.id
    await cache.stop()
