"""Deterministic intent router: grammar coverage and target extraction."""

import pytest

from prodeo_mjolnir.intents import (
    ApproveIntent,
    CancelIntent,
    DenyIntent,
    HelpIntent,
    Intent,
    IntentRouter,
    OvernightIntent,
    PendingIntent,
    RespondIntent,
    StatusIntent,
    StopIntent,
    UnknownIntent,
    normalize,
)

router = IntentRouter()


@pytest.mark.parametrize(
    "text",
    [
        "status",
        "What's the status?",
        "give me a status report",
        "how are things doing",
        "how are my agents",
        "what's happening",
        "what is running",
        # natural phrasings that used to fall through to UnknownIntent
        "do I have any running sessions",
        "do I have any active sessions",
        "are there any running agents",
        "which sessions are running",
    ],
)
@pytest.mark.asyncio
async def test_status_phrasings(text: str) -> None:
    assert await router.route(text) == StatusIntent()


@pytest.mark.parametrize(
    "text",
    [
        "what happened overnight",
        "What happened overnight?",
        "what happened last night",
        "what happened while I was asleep",
        "good morning",
        "Good morning, Mjölnir.",
        "morning briefing",
        "give me the overnight report",
    ],
)
@pytest.mark.asyncio
async def test_overnight_phrasings(text: str) -> None:
    assert await router.route(text) == OvernightIntent()


@pytest.mark.parametrize(
    "text",
    [
        "any questions",
        "anything pending",
        "any permissions waiting",
        "what needs me",
        "anything needs my attention",
        # natural phrasings that used to fall through to UnknownIntent
        "any dialogs waiting for answers",
        "are there any prompts waiting",
        "anything waiting for a response",
        "what's waiting on me",
    ],
)
@pytest.mark.asyncio
async def test_pending_phrasings(text: str) -> None:
    assert await router.route(text) == PendingIntent()


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("approve it", ApproveIntent()),
        ("yes, approve it", ApproveIntent()),
        ("approve", ApproveIntent()),
        ("allow that", ApproveIntent()),
        ("approve the permission", ApproveIntent()),
        (
            "approve the permission for the database migration",
            ApproveIntent(target="database migration"),
        ),
        ("approve the api tests", ApproveIntent(target="api tests")),
        ("deny it", DenyIntent()),
        ("no, deny that", DenyIntent()),
        ("reject the request for the database migration", DenyIntent(target="database migration")),
        ("block it", DenyIntent()),
    ],
)
@pytest.mark.asyncio
async def test_approve_deny_with_targets(text: str, intent: Intent) -> None:
    assert await router.route(text) == intent


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("approve number two", ApproveIntent(target="#2")),
        ("approve the first one", ApproveIntent(target="#1")),
        ("approve one", ApproveIntent(target="#1")),
        ("approve number 3", ApproveIntent(target="#3")),
        ("deny the second one", DenyIntent(target="#2")),
        ("deny number two", DenyIntent(target="#2")),
    ],
)
@pytest.mark.asyncio
async def test_positional_approve_deny(text: str, intent: Intent) -> None:
    assert await router.route(text) == intent


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("respond to two with looks good", RespondIntent(target="#2", text="looks good")),
        ("respond to one saying ship it", RespondIntent(target="#1", text="ship it")),
        ("tell one looks good", RespondIntent(target="#1", text="looks good")),
        (
            "respond to the database migration with go ahead",
            RespondIntent(target="database migration", text="go ahead"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_respond_free_text(text: str, intent: Intent) -> None:
    assert await router.route(text) == intent


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("stop the nightly refactor", StopIntent(target="nightly refactor")),
        ("terminate the claude session", StopIntent(target="claude")),
        ("kill it", StopIntent()),
        ("stop", StopIntent()),
    ],
)
@pytest.mark.asyncio
async def test_stop_targets(text: str, intent: Intent) -> None:
    assert await router.route(text) == intent


@pytest.mark.asyncio
async def test_meta_and_unknown() -> None:
    assert await router.route("help") == HelpIntent()
    assert await router.route("what can you do") == HelpIntent()
    assert await router.route("never mind") == CancelIntent()
    assert await router.route("cancel") == CancelIntent()
    assert await router.route("make me a sandwich") == UnknownIntent(text="make me a sandwich")
    assert await router.route("") == UnknownIntent(text="")
    assert await router.route("...") == UnknownIntent(text="...")


def test_normalize_strips_punctuation_and_case() -> None:
    assert normalize("What's THE   status?!") == "whats the status"
    assert normalize("approve it.") == "approve it"
