# ADR-0012: A constrained LLM as intent classifier, never executor

- **Status**: Accepted
- **Date**: 2026-07-22

## Context
Live voice testing showed the deterministic grammar
([intents.py](../../packages/prodeo-mjolnir/src/prodeo_mjolnir/intents.py))
missing natural phrasings — "do I have any running sessions", "any dialogs
waiting for answers" — that the handlers already answer correctly. The gap is
purely recognition. voice-pipeline.md always anticipated "an LLM intent router
is a plugin upgrade, not a prerequisite"; this is that upgrade.

The risk is obvious: a language model wired into a system that can approve
permissions and terminate sessions must not be able to *decide* to do those
things, hallucinate a target, or act on a phrasing nobody vetted. The user's
constraint was explicit — LLM understanding is wanted, but for "a certain set
of actions, nothing beyond."

## Decision
The LLM is a **classifier over a closed intent set, never an executor**, added
behind the existing `Router` Protocol so the pipeline depends on the seam, not
the implementation. Five properties bound it:

1. **Closed enum.** The model maps speech to exactly one of the frozen `Intent`
   dataclasses (plus an optional free-text `target` *hint*). It can emit nothing
   outside that enum; anything else becomes `UnknownIntent` (no action).
2. **Deterministic-first.** The regex router runs first and answers instantly,
   offline, for every phrasing it covers. The LLM is consulted **only** when the
   grammar returns `UnknownIntent`, so known commands never pay LLM latency and
   the system still works with Ollama absent.
3. **Actions stay deterministic.** For approve/deny/stop the LLM supplies only
   an intent plus a target *hint*; the real target resolution and the
   single-match ambiguity guard remain in the handlers, run against live cache
   data. The LLM never names an interaction or session id.
4. **Allowlist (defense-in-depth).** `MJOLNIR_LLM_INTENTS` defaults to the
   read-only set (`status, pending, overnight, help, cancel`). Action intents
   (`approve, deny, stop`) are emitted **only if** explicitly added. Anything the
   model names outside the allowlist — or outside the known enum — is dropped to
   `UnknownIntent`. A config typo cannot widen the model's authority.
5. **Failure-closed.** Ollama unreachable, a timeout, a non-200, or malformed
   JSON all collapse to `UnknownIntent`, spoken as the ordinary "didn't
   understand" response — mirroring how the persona rephraser already degrades.

The classifier is a single non-streaming Ollama `/api/chat` call
([llm_router.py](../../packages/prodeo-mjolnir/src/prodeo_mjolnir/llm_router.py)),
the same proven pattern as the summarizer plugin, with an injectable `httpx`
transport for tests.

## Consequences
- Natural, unseen phrasings are understood without hand-writing grammar for
  every variant, while the safety envelope (closed enum + allowlist + handler
  guards) means the worst a misclassification can do on the default config is
  answer a read-only query or say "didn't understand".
- Known commands and the fully-offline posture are unchanged: `patterns` stays
  the default; `llm` is opt-in and needs a reachable Ollama.
- The grammar is still the primary surface — it was *also* expanded to cover the
  phrasings testing surfaced, so the common cases stay on the instant offline
  path even without Ollama.
- New cost surface: one bounded LLM round-trip on the fallback path
  (`MJOLNIR_LLM_ROUTER_TIMEOUT_S`, default 4 s), only for utterances the grammar
  missed.

## Alternatives Considered
- **LLM as the primary router.** Rejected: every command would pay LLM latency
  and depend on a running model, losing the offline/latency guarantees, for no
  benefit on the phrasings the grammar already nails.
- **LLM emits structured commands (ids, decisions) directly.** Rejected: it
  would make the model an executor and a target for prompt injection via a
  transcript. Keeping resolution and the ambiguity guard in the handlers means
  the model's output is only ever a hint.
- **A cloud LLM.** Rejected for the satellite/offline posture and to avoid
  sending session context off-device; local Ollama matches the existing
  `summarizer` path.
