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
    ],
)
def test_status_phrasings(text: str) -> None:
    assert router.route(text) == StatusIntent()


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
def test_overnight_phrasings(text: str) -> None:
    assert router.route(text) == OvernightIntent()


@pytest.mark.parametrize(
    "text",
    [
        "any questions",
        "anything pending",
        "any permissions waiting",
        "what needs me",
        "anything needs my attention",
    ],
)
def test_pending_phrasings(text: str) -> None:
    assert router.route(text) == PendingIntent()


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
def test_approve_deny_with_targets(text: str, intent: Intent) -> None:
    assert router.route(text) == intent


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("stop the nightly refactor", StopIntent(target="nightly refactor")),
        ("terminate the claude session", StopIntent(target="claude")),
        ("kill it", StopIntent()),
        ("stop", StopIntent()),
    ],
)
def test_stop_targets(text: str, intent: Intent) -> None:
    assert router.route(text) == intent


def test_meta_and_unknown() -> None:
    assert router.route("help") == HelpIntent()
    assert router.route("what can you do") == HelpIntent()
    assert router.route("never mind") == CancelIntent()
    assert router.route("cancel") == CancelIntent()
    assert router.route("make me a sandwich") == UnknownIntent(text="make me a sandwich")
    assert router.route("") == UnknownIntent(text="")
    assert router.route("...") == UnknownIntent(text="...")


def test_normalize_strips_punctuation_and_case() -> None:
    assert normalize("What's THE   status?!") == "whats the status"
    assert normalize("approve it.") == "approve it"
