# Mjölnir voice fixes: echo suppression, constrained NLU, numbered dialogs

> **Execution notes (exported for the dev container, 2026-07-21):**
> 1. This plan is self-contained — a fresh Claude instance can run it. All
>    file:line references are repo-relative and were verified against the
>    current tree.
> 2. **Container reality check first:** rebuild the dev container so the
>    `UV_PROJECT_ENVIRONMENT` + node_modules-volume isolation in
>    `.devcontainer/devcontainer.json` is active — otherwise `uv sync` inside
>    the container will again clobber the host's `.venv` (this recurred twice).
> 3. **Ollama:** Part B's `llm` mode needs a reachable Ollama. Inside the
>    container that means either `ollama serve` in the container or pointing
>    `MJOLNIR_LLM_ROUTER_BASE_URL` at a host/other Ollama. The default
>    `patterns` mode needs no Ollama, so Parts A/C and the pattern-expansion
>    half of B are fully testable in the container; the `llm` end of B and the
>    voice E2E (real mic/speaker) are best verified on the Windows host.
> 4. Quality gates run from the repo root (`uv run pytest -q`, `uv run mypy`,
>    `uv run ruff …`). Delete this file once merged; the durable record is
>    ADR-0012 (Part E).

## Context

Live voice testing surfaced three problems:

1. **The mic hears Mjölnir's own speech and re-triggers on it.** The pipeline
   is nominally half-duplex, but `SoundDeviceSource` runs a PortAudio callback
   that keeps filling a 64-frame queue *during* playback
   ([audio.py:116-131](packages/prodeo-mjolnir/src/prodeo_mjolnir/audio.py#L116-L131)).
   When the listen loop resumes after speaking, it consumes that buffered echo
   (TTS bleeding speaker→mic), which scores as a wake word → self-triggering.
2. **Natural phrasings aren't understood.** "Do I have any running sessions"
   and "any dialogs waiting for answers" both fall through to `UnknownIntent`
   because the router grammar ([intents.py:83-124](packages/prodeo-mjolnir/src/prodeo_mjolnir/intents.py#L83-L124))
   is narrowly `^…$`-anchored. Confirmed via exploration: the **handlers
   already answer both correctly** (`_status` reads `cache.active_sessions()`,
   `_pending` reads `cache.pending_interactions()`) — the gap is purely intent
   recognition.
3. **Multi-dialog answering is weak.** `PendingIntent` names only the first
   waiting item and offers no positional selector; the user wants "you have 2:
   one… two…" then "approve number two / respond to one with X."

User decisions: (a) LLM understanding is wanted **but risk-constrained** — "a
certain set of actions, nothing beyond"; (b) numbered list + positional
control. So the LLM is a **classifier over a closed intent set**, never an
executor, with a config allowlist gating which intents it may emit.

## Design principles (safety)

- **The LLM only classifies.** It maps speech → one of the existing frozen
  `Intent` dataclasses ([intents.py:66-76](packages/prodeo-mjolnir/src/prodeo_mjolnir/intents.py#L66-L76))
  plus an optional free-text `target`. It can emit **nothing outside that
  enum**; unparseable/low-confidence output → `UnknownIntent` (no action).
- **Deterministic-first.** The regex router runs first (instant, offline). The
  LLM is consulted **only** when it returns `UnknownIntent`, so known phrasings
  never pay LLM latency and the system still works with Ollama absent.
- **Actions stay deterministic.** For `Approve`/`Deny`/`Stop`, the LLM supplies
  only an intent + target *hint*; the real target resolution and single-match
  ambiguity guard remain in the handlers ([handlers.py:170-231](packages/prodeo-mjolnir/src/prodeo_mjolnir/handlers.py#L170-L231))
  against live cache data — the LLM never names an interaction/session id.
- **Allowlist.** `MJOLNIR_LLM_INTENTS` defaults to the read-only set
  (`status, pending, overnight, help, cancel`). Action intents (`approve,
  deny, stop`) are emitted by the LLM **only if** explicitly added. Anything
  the LLM emits outside the allowlist is dropped to `UnknownIntent`. This is
  the "nothing beyond the specified set" guarantee, defense-in-depth on top of
  the closed enum.
- **Failure-closed.** Ollama unreachable/timeout/malformed → `UnknownIntent`,
  spoken as the normal "didn't understand" response. Mirrors how
  `composer.rephrase` already degrades.

## Part A — Echo suppression (the self-trigger bug)

- `audio.py`: hoist `SoundDeviceSource`'s queue to an instance attribute and
  add `drain() -> None` (non-blocking `get_nowait` loop). Add `drain()` to the
  `AudioSource` Protocol as an optional method; the pipeline calls it through a
  `hasattr`/`runtime_checkable` guard so test fakes need no change.
- `pipeline.py` `_listen()`: after **every** `_speak()` returns (both the ack
  at [pipeline.py:148-150](packages/prodeo-mjolnir/src/prodeo_mjolnir/pipeline.py#L148-L150)
  and the response inside `_handle_utterance`), (1) `source.drain()` to discard
  frames buffered during playback, (2) `self._wakeword.reset()`, and (3) set a
  monotonic `mute_until = now + echo_cooldown_s`; while `now < mute_until` and
  no endpointer is active, keep draining and skip wake-word scoring so residual
  room echo can't re-trigger.
- `config.py`: add `echo_cooldown_s: float = 0.4`
  (`MJOLNIR_ECHO_COOLDOWN_S`). Also fold in the previously-recommended capture
  tuning as new defaults so single-utterance commands work: keep `ack_enabled`
  but drain its echo before opening the endpointer; raise `vad_silence_ms`
  default 800 → 1000 (still overridable).

## Part B — Constrained LLM intent router (hybrid)

- **New `packages/prodeo-mjolnir/src/prodeo_mjolnir/llm_router.py`:**
  - `LlmIntentRouter` implementing a new `Router` Protocol (below). Holds a
    fallback `IntentRouter` (deterministic), an Ollama client, the allowed
    intent set, and a timeout.
  - `async def route(self, text) -> Intent`: first `base.route(text)`; if not
    `UnknownIntent`, return it. Else call the LLM with a **strict system
    prompt** enumerating the allowed intents and demanding JSON
    `{"intent": <enum|"unknown">, "target": <string>}`; parse defensively; map
    to the dataclass (reusing `intents._clean_target` for the target); if the
    parsed intent isn't in the allowlist or parsing fails → `UnknownIntent`.
  - A minimal async Ollama client copied from the summarizer's proven pattern
    ([prodeo-summarizer-ollama/__init__.py](packages/prodeo-summarizer-ollama/src/prodeo_summarizer_ollama/src) —
    `POST /api/chat`, `stream:false`, `raise_for_status`, read
    `message.content`), with an injectable `httpx` transport for tests.
- **`intents.py`:** introduce a `Router` `Protocol` with `async def
  route(text) -> Intent`; make `IntentRouter.route` `async` (trivial — body is
  sync). Also **expand `_PATTERNS`** to cover the phrasings the user hit and
  close variants ("do I have any running/active sessions", "which sessions are
  running", "any dialogs/prompts waiting for (a) (response/answer)", "what's
  waiting on me") mapping to existing `status`/`pending` names — this keeps the
  common cases on the instant offline path even without Ollama.
- **`pipeline.py:196`:** `intent = await self._router.route(text)` (seam is now
  async).
- **`main.py` `build_pipeline`:** select the router by config — default
  `IntentRouter()`; if `MJOLNIR_INTENT_ROUTER == "llm"`, construct
  `LlmIntentRouter(base=IntentRouter(), ...)` and pass `router=` into
  `VoicePipeline`.
- **`config.py`:** `intent_router: Literal["patterns","llm"] = "patterns"`;
  `llm_router_base_url = "http://localhost:11434"`;
  `llm_router_model = "llama3.2"`; `llm_router_timeout_s = 4.0`;
  `llm_intents: list[str] = ["status","pending","overnight","help","cancel"]`.

## Part C — Numbered dialogs + positional answering

- **`intents.py`:** extend the approve/deny grammar to accept ordinals
  ("approve number two", "deny the first one", "approve one") capturing a
  positional `target` like `#2`; add a `RespondIntent(target, text)` for
  "respond to two with <text>" / "tell one <text>" (free-text answers to
  question-kind interactions). Include these in the `Intent` union and `route`.
- **`handlers.py`:**
  - `_pending()` → enumerate **all** pending with ordinals using a new
    `pending_list` template (e.g. "N things need you. One: {adapter} on {name}
    asks {title}. Two: …"), and remember the announced ordering on the handler
    (`self._last_pending: list[Interaction]`) so positional references resolve
    against exactly what was read out.
  - `_match_interactions()` gains positional resolution: a `#N`/ordinal target
    selects from `self._last_pending` (falling back to the current sorted
    pending if none announced yet), while name/fuzzy matching is unchanged.
    Single-match ambiguity guard preserved.
  - Add `_respond(target, text)` for `RespondIntent` → resolve the interaction,
    call `client.answer(id, text=text)` (the existing free-text path,
    [client.py:85-99](packages/prodeo-mjolnir/src/prodeo_mjolnir/client.py#L85-L99)),
    respond with a new `responded` template.
- **`packs.py`:** add `pending_list` and `responded` keys to `NEUTRAL` and
  `STEWARD` (the composer hard-errors on missing keys and a test pins pack
  parity, so both packs + the key-set test must be updated together).

## Part D — Tests

- `test_mjolnir_audio.py`: `SoundDeviceSource.drain()` empties buffered frames;
  a fake source with `drain` is exercised.
- `test_mjolnir_pipeline.py`: after a spoken response, buffered echo frames are
  drained and do **not** produce a second wake/utterance; cooldown suppresses
  wake scoring for `echo_cooldown_s`.
- `test_mjolnir_intents.py`: new parametrized phrasings route correctly
  (running/active sessions → Status; dialogs/prompts waiting → Pending;
  ordinals → Approve/Deny with `#N`; "respond to two with …" → RespondIntent).
- **New `test_mjolnir_llm_router.py`** (httpx.MockTransport): deterministic
  hit bypasses the LLM (zero HTTP calls); miss → LLM JSON parsed to the right
  intent; intent outside the allowlist → UnknownIntent; malformed JSON /
  non-200 / timeout / connect error → UnknownIntent; target hint is cleaned and
  fed to the handler matcher, never used as an id.
- `test_mjolnir_handlers.py`: `pending_list` enumeration string; positional
  approve/deny selects the right interaction from the announced list; ambiguity
  and already-resolved paths still hold; `RespondIntent` calls
  `client.answer(id, text=...)`.

## Part E — Docs (same change, per CLAUDE.md)

- New `docs/adr/0012-llm-intent-router.md`: the constrained-classifier design
  (closed enum, allowlist, deterministic-first, failure-closed, actions stay
  deterministic), referencing the voice-pipeline.md "LLM router is a plugin
  upgrade" statement it fulfils.
- `docs/architecture/voice-pipeline.md`: intent section — deterministic fast
  path + optional LLM fallback, the safety envelope, echo-suppression note.
- `packages/prodeo-mjolnir/README.md`: new env vars (`MJOLNIR_INTENT_ROUTER`,
  `MJOLNIR_LLM_*`, `MJOLNIR_LLM_INTENTS`, `MJOLNIR_ECHO_COOLDOWN_S`), Ollama
  prerequisite for `llm` mode, and the expanded "what you can say" list
  including numbered answering.

## Verification

```bash
uv run pytest -q packages/prodeo-mjolnir && uv run mypy && uv run ruff check . && uv run ruff format --check .
```

Manual E2E (server + mjolnir on host, per prior sessions):
1. Echo: ask something that yields a long spoken reply; confirm Mjölnir does
   **not** wake itself afterward (watch for a stray `voice.wake_word_detected`
   in the logs; there should be none from its own speech).
2. Patterns offline: with `MJOLNIR_INTENT_ROUTER=patterns`, say "do I have any
   running sessions" → status answer; "any dialogs waiting for answers" →
   pending answer.
3. LLM mode: `ollama serve` + `ollama pull llama3.2`, set
   `MJOLNIR_INTENT_ROUTER=llm`; say a novel phrasing ("what's cooking with my
   agents?") → correct intent; say something mapping to a non-allowlisted
   action → politely not understood (no action taken).
4. Numbered: with ≥2 pending interactions, "anything need me?" → enumerated
   list; "approve number two" → the second announced item is answered (verify
   via dashboard); "respond to one with looks good" → free-text answer posted.

## Risks

- LLM latency on the fallback path (bounded by `llm_router_timeout_s=4`,
  failure-closed to UnknownIntent). Known phrasings never hit it.
- Misclassification of action verbs: mitigated by keeping deterministic
  approve/deny patterns primary, the allowlist defaulting action intents OFF,
  and the handler's single-match guard. Stop/terminate stays off-by-default via
  voice.
- Positional-reference races (a pending item resolves between announce and
  command): resolved against the remembered `_last_pending`; a vanished item
  falls back to the ambiguity/not-found responses rather than mis-answering.
- Echo drain must not swallow a genuine immediate follow-up: cooldown is short
  (0.4s) and only active right after Mjölnir speaks.
