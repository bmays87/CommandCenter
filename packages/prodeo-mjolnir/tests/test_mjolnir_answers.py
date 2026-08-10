"""AnswerEngine: grounded QA for utterances no intent matched (ADR-0018).

Every HTTP interaction is faked with ``httpx.MockTransport`` - no Ollama runs.
What matters here: the frame of reference and live state reach the model, the
answer reaches the speaker, and every failure collapses to "" so the handlers
fall back to the deterministic template.
"""

import json

import httpx
import pytest
from mjolnir_fakes import FakeServerClient, make_interaction, make_session, started_cache

from prodeo.sessions import SessionState
from prodeo_mjolnir.answers import AnswerEngine
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.handlers import CommandHandlers
from prodeo_mjolnir.intents import UnknownIntent
from prodeo_mjolnir.packs import NEUTRAL

pytestmark = pytest.mark.asyncio


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": content}})


def _engine(
    cache: LocalCache,
    handler: httpx.MockTransport,
    *,
    honorific: str = "",
) -> AnswerEngine:
    return AnswerEngine(
        cache,
        base_url="http://ollama.test",
        model="llama3.2",
        honorific=honorific,
        transport=handler,
    )


async def _populated_cache() -> LocalCache:
    client = FakeServerClient()
    client.sessions = [
        make_session("s1", title="nightly-refactor", active_ago_s=120),
        make_session("s2", project="/repos/api-tests", state=SessionState.WAITING_ON_USER),
        make_session("s3", project="/old-thing", state=SessionState.COMPLETED, active_ago_s=3600),
    ]
    client.interactions = [make_interaction("i1", "s2", title="Run the migration?")]
    return await started_cache(client)


async def test_the_model_receives_identity_and_live_state() -> None:
    """The frame of reference: who it is, what agents are, what is happening."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply("Two agents are working.")

    engine = _engine(await _populated_cache(), httpx.MockTransport(handler))
    spoken = await engine.answer("what are you and what's going on")

    assert spoken == "Two agents are working."
    (body,) = seen
    system = body["messages"][0]["content"]
    # Identity and purpose...
    assert "Mjölnir" in system
    assert "coding agent" in system
    # ...the command vocabulary it can point users at...
    assert "what's the status" in system
    # ...and the live picture, by name, with the pending request.
    assert "Active sessions (2):" in system
    assert "nightly-refactor" in system
    assert "api-tests" in system
    assert "Run the migration?" in system
    assert "number 1" in system
    # The utterance is the user message, untouched.
    assert body["messages"][1] == {"role": "user", "content": "what are you and what's going on"}


async def test_empty_state_is_stated_not_omitted() -> None:
    """ "None" must be a fact the model is told, or it will invent activity."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply("All quiet.")

    cache = await started_cache(FakeServerClient())
    await _engine(cache, httpx.MockTransport(handler)).answer("anything happening?")

    system = seen[0]["messages"][0]["content"]
    assert "Active sessions: none." in system
    assert "Pending permission requests: none." in system


async def test_honorific_reaches_the_prompt() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply("Certainly, sir.")

    cache = await started_cache(FakeServerClient())
    await _engine(cache, httpx.MockTransport(handler), honorific="sir").answer("hello")

    assert "Address the user as sir." in seen[0]["messages"][0]["content"]


async def test_llm_failure_answers_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    cache = await started_cache(FakeServerClient())
    assert await _engine(cache, httpx.MockTransport(handler)).answer("hello") == ""


async def test_unreachable_llm_answers_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    cache = await started_cache(FakeServerClient())
    assert await _engine(cache, httpx.MockTransport(handler)).answer("hello") == ""


# --- through the handlers ----------------------------------------------------


class _ScriptedAnswerer:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.asked: list[str] = []

    async def answer(self, text: str) -> str:
        self.asked.append(text)
        return self.reply


async def _handlers(answers: _ScriptedAnswerer | None) -> CommandHandlers:
    client = FakeServerClient()
    cache = await started_cache(client)
    composer = ResponseComposer(NEUTRAL, honorific="sir")
    return CommandHandlers(cache, client.as_client(), composer, answers=answers)


async def test_unknown_intent_is_answered_as_a_question() -> None:
    answers = _ScriptedAnswerer("Agents are AI programming assistants working for you.")
    handlers = await _handlers(answers)

    spoken = await handlers.handle(UnknownIntent(text="what is an agent"))

    assert spoken == "Agents are AI programming assistants working for you."
    assert answers.asked == ["what is an agent"]


async def test_empty_answer_falls_back_to_the_template() -> None:
    handlers = await _handlers(_ScriptedAnswerer(""))
    spoken = await handlers.handle(UnknownIntent(text="what is an agent"))
    assert spoken == "Sorry, sir, I didn't understand: what is an agent."


async def test_without_an_engine_the_template_speaks_as_before() -> None:
    handlers = await _handlers(None)
    spoken = await handlers.handle(UnknownIntent(text="what is an agent"))
    assert spoken == "Sorry, sir, I didn't understand: what is an agent."


async def test_blank_utterance_never_reaches_the_engine() -> None:
    answers = _ScriptedAnswerer("should not be spoken")
    handlers = await _handlers(answers)

    spoken = await handlers.handle(UnknownIntent(text="  "))

    assert answers.asked == []
    assert "didn't understand" in spoken
