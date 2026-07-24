"""Constrained LLM intent router: deterministic-first, closed enum, fail-closed.

Every HTTP interaction is faked with ``httpx.MockTransport`` - no Ollama runs.
"""

import json

import httpx
import pytest

from prodeo_mjolnir.intents import (
    ApproveIntent,
    IntentRouter,
    PendingIntent,
    StatusIntent,
    UnknownIntent,
)
from prodeo_mjolnir.llm_router import LlmIntentRouter

pytestmark = pytest.mark.asyncio


def _reply(intent: str, target: str = "") -> httpx.Response:
    """One Ollama ``/api/chat`` response carrying the classifier's JSON."""
    content = json.dumps({"intent": intent, "target": target})
    return httpx.Response(200, json={"message": {"content": content}})


def _router(
    handler: httpx.MockTransport,
    *,
    allowed: set[str] | None = None,
    timeout_s: float = 4.0,
) -> LlmIntentRouter:
    return LlmIntentRouter(
        base=IntentRouter(),
        base_url="http://ollama.test",
        model="llama3.2",
        allowed=allowed or {"status", "pending", "overnight", "help", "cancel"},
        timeout_s=timeout_s,
        transport=handler,
    )


async def test_deterministic_hit_never_calls_the_llm() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _reply("status")

    router = _router(httpx.MockTransport(handler))
    assert await router.route("what's the status") == StatusIntent()
    assert calls == []  # the grammar answered; the model was never consulted


async def test_miss_is_classified_by_the_llm() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _reply("pending")

    router = _router(httpx.MockTransport(handler))
    # A phrasing the deterministic grammar does not cover.
    assert await router.route("what's cooking with my agents") == PendingIntent()


async def test_target_hint_is_cleaned_and_passed_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _reply("approve", target="the database migration")

    router = _router(httpx.MockTransport(handler), allowed={"status", "approve"})
    intent = await router.route("give the migration a thumbs up")
    # The hint is cleaned like any target; the LLM never supplies an id.
    assert intent == ApproveIntent(target="database migration")


async def test_intent_outside_the_allowlist_is_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _reply("stop", target="everything")  # action not on the allowlist

    router = _router(httpx.MockTransport(handler), allowed={"status", "pending"})
    result = await router.route("shut it all down please")
    assert isinstance(result, UnknownIntent)


async def test_unknown_from_the_model_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _reply("unknown")

    router = _router(httpx.MockTransport(handler))
    assert isinstance(await router.route("sing me a song"), UnknownIntent)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"message": {"content": "not json at all"}}),
        httpx.Response(200, json={"message": {"content": '{"intent": }'}}),  # malformed
        httpx.Response(200, json={"message": {"content": "[1, 2, 3]"}}),  # not an object
        httpx.Response(500, text="model exploded"),  # non-200
    ],
)
async def test_bad_responses_fail_closed(response: httpx.Response) -> None:
    router = _router(httpx.MockTransport(lambda request: response))
    assert isinstance(await router.route("some novel phrasing"), UnknownIntent)


async def test_timeout_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    router = _router(httpx.MockTransport(handler))
    assert isinstance(await router.route("some novel phrasing"), UnknownIntent)


async def test_connect_error_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no ollama here", request=request)

    router = _router(httpx.MockTransport(handler))
    result = await router.route("some novel phrasing")
    assert result == UnknownIntent(text="some novel phrasing")
