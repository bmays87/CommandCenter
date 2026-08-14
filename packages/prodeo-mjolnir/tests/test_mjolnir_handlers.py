"""CommandHandlers: intents against the cache, commands through the client.

Conversational rules under test (ADR-0023): replies say when they expect a
follow-up, ambiguity opens a choose dialog instead of dead-ending, voice
launches are slot-filled and **confirm-first** (no path launches without an
explicit yes), and an announced question's options are answerable directly.
"""

import pytest
from mjolnir_fakes import (
    FakeServerClient,
    make_adapter_info,
    make_interaction,
    make_session,
    started_cache,
)

from prodeo.mediation import InteractionKind, QuestionGroup, QuestionOption
from prodeo.sessions import SessionState
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.handlers import CommandHandlers
from prodeo_mjolnir.intents import (
    ApproveIntent,
    DenyIntent,
    Intent,
    LaunchIntent,
    OvernightIntent,
    PendingIntent,
    RespondIntent,
    StatusIntent,
    StopIntent,
    UnknownIntent,
)
from prodeo_mjolnir.packs import NEUTRAL


class Clock:
    """A hand-cranked monotonic clock for dialog-TTL tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _handlers(
    client: FakeServerClient,
    *,
    launch_adapter: str = "",
    clock: Clock | None = None,
    dialog_ttl_s: float = 90.0,
) -> CommandHandlers:
    cache = await started_cache(client)
    return CommandHandlers(
        cache,
        client.as_client(),
        ResponseComposer(NEUTRAL, honorific="sir"),
        launch_adapter=launch_adapter,
        dialog_ttl_s=dialog_ttl_s,
        clock=clock,
    )


async def say(handlers: CommandHandlers, intent: Intent) -> str:
    return (await handlers.handle(intent)).text


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

    text = await say(handlers, StatusIntent())
    assert text == (
        "2 sessions active, sir: nightly-refactor and api-tests. "
        "1 interaction awaiting your answer."
    )

    client.sessions = []
    client.interactions = []
    empty = await _handlers(FakeServerClient())
    assert await say(empty, StatusIntent()) == "No sessions are active, sir."


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

    text = await say(handlers, OvernightIntent())
    assert "3 agent sessions ran while you were away, sir." in text
    assert "nightly-refactor finished." in text
    assert "api-tests failed." in text
    assert "db is waiting on you: May I run the database migration?" in text


@pytest.mark.asyncio
async def test_overnight_quiet() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("old", state=SessionState.COMPLETED, active_ago_s=3600 * 24)]
    handlers = await _handlers(client)
    assert await say(handlers, OvernightIntent()) == (
        "All quiet, sir. No agent activity in the last 12 hours."
    )


@pytest.mark.asyncio
async def test_pending_none_one_many() -> None:
    client = FakeServerClient()
    handlers = await _handlers(client)
    assert await say(handlers, PendingIntent()) == "Nothing is waiting on you, sir."

    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Run it?")]
    handlers = await _handlers(client)
    assert await say(handlers, PendingIntent()) == (
        "One thing needs you, sir. claude-code on db asks: Run it?"
    )


@pytest.mark.asyncio
async def test_approve_single_pending_needs_no_target() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Run the migration?")]
    handlers = await _handlers(client)

    assert await say(handlers, ApproveIntent()) == "Approved, sir."
    assert client.answered == [("i1", "allow")]


@pytest.mark.asyncio
async def test_approve_with_many_pending_and_no_target_invites_a_reply() -> None:
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

    # several pending + no target: read them out, keep listening - the
    # positional follow-up ("approve number two") needs no re-wake.
    reply = await handlers.handle(ApproveIntent())
    assert reply.text.startswith("2 things need you, sir.")
    assert reply.expect_reply is True
    assert client.answered == []
    assert handlers.dialog_pending is False  # normal routing handles the follow-up

    assert await say(handlers, ApproveIntent(target="database migration")) == "Approved, sir."
    assert client.answered == [("i1", "allow")]

    assert await say(handlers, DenyIntent(target="fixtures")) == "Denied, sir."
    assert client.answered == [("i1", "allow"), ("i2", "deny")]

    assert "couldn't find" in await say(handlers, ApproveIntent(target="the moon lander"))


@pytest.mark.asyncio
async def test_ambiguous_target_opens_a_choose_dialog() -> None:
    """An ambiguous name no longer dead-ends: Mjölnir asks which one."""
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/api-tests"),
        make_session("s2", project="/repos/api-docs"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the tests?"),
        make_interaction("i2", "s2", title="Publish the docs?"),
    ]
    handlers = await _handlers(client)

    reply = await handlers.handle(ApproveIntent(target="api"))
    assert "Which one, sir?" in reply.text
    assert "One: api-tests" in reply.text and "Two: api-docs" in reply.text
    assert reply.expect_reply is True
    assert handlers.dialog_pending is True
    assert client.answered == []  # nothing acted on yet

    resumed = await handlers.resume("the second one")
    assert resumed is not None
    assert resumed.text == "Approved, sir."
    assert client.answered == [("i2", "allow")]
    assert handlers.dialog_pending is False


@pytest.mark.asyncio
async def test_choose_dialog_ordinal_binds_to_the_dialog_not_last_pending() -> None:
    """The ordinal-collision pin: while a choose dialog is pending, "two"
    means the dialog's second candidate - never ``_last_pending``'s."""
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/db"),
        make_session("s2", project="/repos/api-tests"),
        make_session("s3", project="/repos/api-docs"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the migration?"),
        make_interaction("i2", "s2", title="Run the tests?"),
        make_interaction("i3", "s3", title="Publish the docs?"),
    ]
    handlers = await _handlers(client)

    await handlers.handle(PendingIntent())  # _last_pending = [i1, i2, i3]
    reply = await handlers.handle(ApproveIntent(target="api"))  # matches i2 + i3
    assert reply.expect_reply is True

    resumed = await handlers.resume("two")
    assert resumed is not None and resumed.text == "Approved, sir."
    assert client.answered == [("i3", "allow")]  # dialog's #2, not pending's #2


@pytest.mark.asyncio
async def test_choose_dialog_contradicting_verb_falls_back_to_routing() -> None:
    """ "Deny number two" while disambiguating an approve must not approve."""
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/api-tests"),
        make_session("s2", project="/repos/api-docs"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the tests?"),
        make_interaction("i2", "s2", title="Publish the docs?"),
    ]
    handlers = await _handlers(client)

    await handlers.handle(ApproveIntent(target="api"))
    assert handlers.dialog_pending is True

    resumed = await handlers.resume("deny number two")
    assert resumed is None  # hand back to normal routing
    assert handlers.dialog_pending is False
    assert client.answered == []


@pytest.mark.asyncio
async def test_choose_dialog_reasks_once_then_cancels() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/api-tests"),
        make_session("s2", project="/repos/api-docs"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the tests?"),
        make_interaction("i2", "s2", title="Publish the docs?"),
    ]
    handlers = await _handlers(client)

    await handlers.handle(ApproveIntent(target="api"))
    first = await handlers.resume("hmm the green one")
    assert first is not None and "Which one, sir?" in first.text
    second = await handlers.resume("the green one I said")
    assert second is not None and second.text == "Very well, sir."
    assert handlers.dialog_pending is False
    assert client.answered == []


@pytest.mark.asyncio
async def test_dialog_expires_after_ttl() -> None:
    clock = Clock()
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/api-tests"),
        make_session("s2", project="/repos/api-docs"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the tests?"),
        make_interaction("i2", "s2", title="Publish the docs?"),
    ]
    handlers = await _handlers(client, clock=clock, dialog_ttl_s=90.0)

    await handlers.handle(ApproveIntent(target="api"))
    assert handlers.dialog_pending is True
    clock.now += 91.0
    assert handlers.dialog_pending is False


@pytest.mark.asyncio
async def test_approve_lost_race_is_reported_gracefully() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Run it?")]
    client.already_resolved.add("i1")
    handlers = await _handlers(client)
    assert await say(handlers, ApproveIntent()) == "That was already answered elsewhere, sir."


@pytest.mark.asyncio
async def test_stop_by_name_and_ambiguity() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", title="nightly-refactor", active_ago_s=30),  # most recent: first
        make_session("s2", project="/repos/api-tests", active_ago_s=600),
    ]
    handlers = await _handlers(client)

    assert await say(handlers, StopIntent(target="nightly refactor")) == (
        "nightly-refactor has been stopped, sir."
    )
    assert client.terminated == ["s1"]

    # two active, no target: ask which one, then stop the chosen ordinal
    reply = await handlers.handle(StopIntent())
    assert "Which one, sir?" in reply.text and reply.expect_reply is True
    resumed = await handlers.resume("number two")
    assert resumed is not None and "api-tests has been stopped" in resumed.text
    assert client.terminated == ["s1", "s2"]

    assert "couldn't find" in await say(handlers, StopIntent(target="ghost"))


@pytest.mark.asyncio
async def test_unknown_echoes_the_transcript() -> None:
    handlers = await _handlers(FakeServerClient())
    text = await say(handlers, UnknownIntent(text="make me a sandwich"))
    assert text == "Sorry, sir, I didn't understand: make me a sandwich."


@pytest.mark.asyncio
async def test_pending_enumerates_all_with_ordinals() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/db"),
        make_session("s2", project="/repos/api"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the migration?"),
        make_interaction("i2", "s2", title="Delete fixtures?"),
    ]
    handlers = await _handlers(client)

    text = await say(handlers, PendingIntent())
    assert text == (
        "2 things need you, sir. "
        "One: claude-code on db asks: Run the migration? "
        "Two: claude-code on api asks: Delete fixtures?"
    )


@pytest.mark.asyncio
async def test_positional_answer_targets_the_announced_item() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/db"),
        make_session("s2", project="/repos/api"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the migration?"),
        make_interaction("i2", "s2", title="Delete fixtures?"),
    ]
    handlers = await _handlers(client)

    await handlers.handle(PendingIntent())  # announce the ordering first
    assert await say(handlers, ApproveIntent(target="#2")) == "Approved, sir."
    assert await say(handlers, DenyIntent(target="#1")) == "Denied, sir."
    assert client.answered == [("i2", "allow"), ("i1", "deny")]

    # a position past the end is a clean "not found", not a mis-answer
    assert "couldn't find" in await say(handlers, ApproveIntent(target="#5"))


@pytest.mark.asyncio
async def test_positional_falls_back_to_current_pending_when_unannounced() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Run it?")]
    handlers = await _handlers(client)

    # no prior announcement: #1 resolves against the current sorted pending
    assert await say(handlers, ApproveIntent(target="#1")) == "Approved, sir."
    assert client.answered == [("i1", "allow")]


@pytest.mark.asyncio
async def test_respond_posts_free_text_answer() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Which branch?")]
    handlers = await _handlers(client)

    assert await say(handlers, RespondIntent(text="the main branch")) == "Answered, sir."
    assert client.answered == [("i1", None)]  # a text answer, no allow/deny decision
    assert client.answered_text == [("i1", "the main branch")]


@pytest.mark.asyncio
async def test_respond_by_position_after_announcement() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="/repos/db"),
        make_session("s2", project="/repos/api"),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Which branch?"),
        make_interaction("i2", "s2", title="How many workers?"),
    ]
    handlers = await _handlers(client)

    await handlers.handle(PendingIntent())
    assert await say(handlers, RespondIntent(target="#2", text="four")) == "Answered, sir."
    assert client.answered_text == [("i2", "four")]

    # already answered elsewhere: reported, never a false success
    client.already_resolved.add("i1")
    assert await say(handlers, RespondIntent(target="#1", text="main")) == (
        "That was already answered elsewhere, sir."
    )


@pytest.mark.asyncio
async def test_respond_to_multipart_question_needs_the_dashboard() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [
        make_interaction(
            "i1",
            "s1",
            title="Which way? (+1 more)",
            kind=InteractionKind.QUESTION,
            questions=[
                QuestionGroup(id="a", prompt="Which way?", options=[QuestionOption(label="L")]),
                QuestionGroup(id="b", prompt="Extras?", options=[QuestionOption(label="X")]),
            ],
        )
    ]
    handlers = await _handlers(client)

    text = await say(handlers, RespondIntent(text="left"))
    assert "needs the dashboard" in text
    assert client.answered_text == []


# ------------------------------------------------------------- voice launch


def _launch_ready_client() -> FakeServerClient:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="F:/repos/CommandCenter", state=SessionState.COMPLETED),
        make_session("s2", project="F:/repos/paintball", state=SessionState.COMPLETED),
    ]
    return client


@pytest.mark.asyncio
async def test_launch_full_slots_confirms_then_launches_on_yes() -> None:
    client = _launch_ready_client()
    handlers = await _handlers(client)

    reply = await handlers.handle(LaunchIntent(project="command center", prompt="fix the tests"))
    assert reply.text == (
        "Starting an agent on CommandCenter to: fix the tests. Shall I go ahead, sir?"
    )
    assert reply.expect_reply is True
    assert client.launched == []  # NOTHING launched before the yes

    resumed = await handlers.resume("yes go ahead")
    assert resumed is not None
    assert resumed.text == "Underway, sir. The agent is working on CommandCenter."
    assert client.launched == [
        {"adapter": "claude-code", "project": "F:/repos/CommandCenter", "prompt": "fix the tests"}
    ]


@pytest.mark.asyncio
async def test_launch_no_answer_is_never_a_launch() -> None:
    """The confirm-first invariant: deny, silence-equivalent, and off-topic
    replies all leave nothing launched."""
    client = _launch_ready_client()
    handlers = await _handlers(client)

    await handlers.handle(LaunchIntent(project="paintball", prompt="add scoring"))
    resumed = await handlers.resume("no")
    assert resumed is not None and resumed.text == "Launch cancelled, sir."
    assert client.launched == []

    await handlers.handle(LaunchIntent(project="paintball", prompt="add scoring"))
    off_topic = await handlers.resume("whats the status")
    assert off_topic is None  # rerouted as a fresh intent, dialog cleared
    assert handlers.dialog_pending is False
    assert client.launched == []

    await handlers.handle(LaunchIntent(project="paintball", prompt="add scoring"))
    cancelled = await handlers.resume("never mind")
    assert cancelled is not None and cancelled.text == "Very well, sir."
    assert client.launched == []


@pytest.mark.asyncio
async def test_launch_unknown_project_asks_and_accepts_a_known_one() -> None:
    client = _launch_ready_client()
    handlers = await _handlers(client)

    reply = await handlers.handle(LaunchIntent(project="moon lander", prompt="land"))
    assert "I don't know a project matching moon lander" in reply.text
    assert reply.expect_reply is True

    named = await handlers.resume("paintball")
    assert named is not None
    assert "Starting an agent on paintball to: land." in named.text
    confirmed = await handlers.resume("yes")
    assert confirmed is not None
    assert client.launched[0]["project"] == "F:/repos/paintball"


@pytest.mark.asyncio
async def test_launch_unknown_project_twice_gives_up() -> None:
    client = _launch_ready_client()
    handlers = await _handlers(client)

    await handlers.handle(LaunchIntent(project="moon lander", prompt="land"))
    again = await handlers.resume("mars rover")
    assert again is not None and "I don't know a project matching mars rover" in again.text
    gave_up = await handlers.resume("venus probe")
    assert gave_up is not None and gave_up.text == "Very well, sir."
    assert handlers.dialog_pending is False
    assert client.launched == []


@pytest.mark.asyncio
async def test_launch_missing_prompt_is_slot_filled_verbatim() -> None:
    client = _launch_ready_client()
    handlers = await _handlers(client)

    reply = await handlers.handle(LaunchIntent(project="paintball"))
    assert reply.text == "What should the agent do on paintball, sir?"
    assert reply.expect_reply is True

    confirmed = await handlers.resume("Add a scoreboard to the lobby screen")
    assert confirmed is not None
    assert "Add a scoreboard to the lobby screen" in confirmed.text  # verbatim, not normalized
    final = await handlers.resume("yes")
    assert final is not None
    assert client.launched[0]["prompt"] == "Add a scoreboard to the lobby screen"


@pytest.mark.asyncio
async def test_launch_ambiguous_project_offers_a_choice() -> None:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", project="F:/repos/api-tests", state=SessionState.COMPLETED),
        make_session("s2", project="F:/repos/api-docs", state=SessionState.COMPLETED),
    ]
    handlers = await _handlers(client)

    reply = await handlers.handle(LaunchIntent(project="api", prompt="update deps"))
    assert "Which one, sir?" in reply.text
    # candidates are sorted: One: api-docs, Two: api-tests
    chosen = await handlers.resume("the second one")
    assert chosen is not None and "Starting an agent on api-tests" in chosen.text
    final = await handlers.resume("yes")
    assert final is not None
    assert client.launched[0]["project"] == "F:/repos/api-tests"


@pytest.mark.asyncio
async def test_launch_adapter_resolution_and_override() -> None:
    client = _launch_ready_client()
    client.adapters = [
        make_adapter_info("claude-code", launch=True),
        make_adapter_info("watcher", launch=False),  # observe-only: never offered
        make_adapter_info("aider", launch=True),
    ]
    handlers = await _handlers(client)

    reply = await handlers.handle(LaunchIntent(project="paintball", prompt="add scoring"))
    assert "Which one, sir?" in reply.text
    assert "watcher" not in reply.text
    picked = await handlers.resume("aider")
    assert picked is not None and "Shall I go ahead, sir?" in picked.text
    final = await handlers.resume("yes")
    assert final is not None
    assert client.launched[0]["adapter"] == "aider"

    # a configured adapter skips the question entirely
    override = await _handlers(_launch_ready_client(), launch_adapter="claude-code")
    direct = await override.handle(LaunchIntent(project="paintball", prompt="add scoring"))
    assert "Shall I go ahead, sir?" in direct.text


@pytest.mark.asyncio
async def test_launch_failure_is_spoken_not_raised() -> None:
    client = _launch_ready_client()
    client.launch_error = "adapter exploded"
    handlers = await _handlers(client)

    await handlers.handle(LaunchIntent(project="paintball", prompt="add scoring"))
    resumed = await handlers.resume("yes")
    assert resumed is not None
    assert resumed.text == "I couldn't start it, sir: adapter exploded."


# ------------------------------------------------- announced question context


@pytest.mark.asyncio
async def test_announced_question_answered_by_label_and_ordinal() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [
        make_interaction(
            "i1",
            "s1",
            title="Which approach?",
            kind=InteractionKind.QUESTION,
            options=["Safe path", "Fast path"],
        )
    ]
    handlers = await _handlers(client)

    handlers.note_announced_interaction("i1")
    reply = await handlers.handle(UnknownIntent(text="the fast path"))
    assert reply.text == "Answered, sir."
    assert client.answered_text == [("i1", "Fast path")]

    # ordinal form on a fresh announcement
    client.answered_text.clear()
    handlers.note_announced_interaction("i1")
    reply = await handlers.handle(UnknownIntent(text="option one"))
    assert reply.text == "Answered, sir."
    assert client.answered_text == [("i1", "Safe path")]


@pytest.mark.asyncio
async def test_announced_question_no_match_falls_through_to_unknown() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [
        make_interaction(
            "i1",
            "s1",
            title="Which approach?",
            kind=InteractionKind.QUESTION,
            options=["Safe path", "Fast path"],
        )
    ]
    handlers = await _handlers(client)

    handlers.note_announced_interaction("i1")
    reply = await handlers.handle(UnknownIntent(text="tell me a joke about compilers"))
    assert reply.text.startswith("Sorry, sir")  # ADR-0018 ordering preserved
    assert client.answered_text == []


@pytest.mark.asyncio
async def test_announced_multipart_question_points_to_the_dashboard() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [
        make_interaction(
            "i1",
            "s1",
            title="Which way? (+1 more)",
            kind=InteractionKind.QUESTION,
            questions=[
                QuestionGroup(id="a", prompt="Which way?", options=[QuestionOption(label="L")]),
                QuestionGroup(
                    id="b",
                    prompt="Extras?",
                    options=[QuestionOption(label="X")],
                    multi_select=True,
                ),
            ],
        )
    ]
    handlers = await _handlers(client)

    handlers.note_announced_interaction("i1")
    reply = await handlers.handle(UnknownIntent(text="left"))
    assert "needs the dashboard" in reply.text
    assert client.answered_text == []


@pytest.mark.asyncio
async def test_announced_permission_is_not_option_matched() -> None:
    """Only question-kind announcements are answerable by option label."""
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    client.interactions = [make_interaction("i1", "s1", title="Run it?")]
    handlers = await _handlers(client)

    handlers.note_announced_interaction("i1")
    reply = await handlers.handle(UnknownIntent(text="one"))
    assert reply.text.startswith("Sorry, sir")
    assert client.answered == []
