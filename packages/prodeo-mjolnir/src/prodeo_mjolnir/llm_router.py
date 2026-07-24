"""Constrained LLM intent router (ADR-0012): a classifier, never an executor.

The deterministic :class:`~prodeo_mjolnir.intents.IntentRouter` runs first and
answers instantly, offline, for every phrasing its grammar covers. Only when it
returns :class:`UnknownIntent` is the LLM consulted, and even then the LLM's
sole job is to pick **one** intent from a frozen, allow-listed set (plus an
optional free-text ``target`` *hint*). It can emit nothing outside that set;
anything else - a novel intent name, malformed JSON, a timeout, an unreachable
Ollama - fails closed to :class:`UnknownIntent`, spoken as the ordinary "didn't
understand" response. Target resolution and the ambiguity guard stay in the
handlers against live cache data; the LLM never names an id.
"""

import json
import re
from typing import Any

import httpx
import structlog

from prodeo_mjolnir.intents import (
    ApproveIntent,
    CancelIntent,
    DenyIntent,
    HelpIntent,
    Intent,
    OvernightIntent,
    PendingIntent,
    Router,
    StatusIntent,
    StopIntent,
    UnknownIntent,
    _clean_target,
    normalize,
)

_log = structlog.get_logger(__name__)

#: Intents the classifier can name that take no target.
_NO_TARGET: dict[str, type[Intent]] = {
    "status": StatusIntent,
    "pending": PendingIntent,
    "overnight": OvernightIntent,
    "help": HelpIntent,
    "cancel": CancelIntent,
}
#: Action intents; the target is only a hint the handlers resolve.
_WITH_TARGET: dict[str, type[Intent]] = {
    "approve": ApproveIntent,
    "deny": DenyIntent,
    "stop": StopIntent,
}
#: Every intent name the classifier is allowed to know about.
_KNOWN = _NO_TARGET.keys() | _WITH_TARGET.keys()

_SYSTEM_PROMPT = (
    "You are the intent classifier for a voice assistant that oversees coding "
    "agents. Map the user's utterance to exactly one intent from this set: {intents}. "
    'Reply with ONLY a JSON object, no prose: {{"intent": "<one of the intents or '
    'unknown>", "target": "<optional free-text naming which session or request, '
    'else empty>"}}. Never invent an id. If the utterance does not clearly match one '
    'of the listed intents, answer {{"intent": "unknown", "target": ""}}.'
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class LlmIntentRouter:
    """Deterministic-first router with a constrained LLM fallback."""

    def __init__(
        self,
        *,
        base: Router,
        base_url: str,
        model: str,
        allowed: set[str],
        timeout_s: float = 4.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base
        self._base_url = base_url.rstrip("/")
        self._model = model
        #: Defense-in-depth: even if the model names something else, only these
        #: pass. Names outside the known set are ignored (a config typo can't
        #: widen the LLM's authority beyond the frozen enum).
        self._allowed = allowed & _KNOWN
        self._timeout_s = timeout_s
        self._transport = transport

    async def route(self, text: str) -> Intent:
        base_intent = await self._base.route(text)
        if not isinstance(base_intent, UnknownIntent):
            return base_intent
        try:
            content = await self._classify(text)
        except Exception as exc:  # unreachable / timeout / non-200: fail closed
            _log.warning("llm_router.unavailable", error=str(exc))
            return UnknownIntent(text=normalize(text))
        return self._parse(content, text)

    async def _classify(self, text: str) -> str:
        system = _SYSTEM_PROMPT.format(intents=", ".join(sorted(self._allowed)))
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "format": "json",
        }
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout_s, transport=self._transport
        ) as client:
            response = await client.post("/api/chat", json=body)
            response.raise_for_status()
            data = response.json()
        return str(data.get("message", {}).get("content", ""))

    def _parse(self, content: str, original: str) -> Intent:
        """Map the model's JSON to an allow-listed intent; anything off the
        list (or unparseable) becomes UnknownIntent."""
        match = _JSON_OBJECT.search(content)
        if match is None:
            return self._unknown(original)
        try:
            obj = json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            return self._unknown(original)
        if not isinstance(obj, dict):
            return self._unknown(original)
        name = str(obj.get("intent", "")).strip().lower()
        target = _clean_target(str(obj.get("target", "")))
        if name not in self._allowed:
            return self._unknown(original)
        if name in _WITH_TARGET:
            return _WITH_TARGET[name](target=target)  # type: ignore[call-arg]
        return _NO_TARGET[name]()

    def _unknown(self, original: str) -> Intent:
        return UnknownIntent(text=normalize(original))
