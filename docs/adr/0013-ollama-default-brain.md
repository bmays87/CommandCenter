# ADR-0013: Ollama is Mjölnir's default LLM brain and personality

- **Status**: Accepted
- **Date**: 2026-07-24

## Context
[ADR-0012](0012-llm-intent-router.md) added a constrained LLM intent router but
left it opt-in (`intent_router="patterns"`), and the persona rephraser shipped
off (`persona_rephraser=""`). Out of the box, then, Mjölnir only understood
hand-written grammar and spoke flat template text — effectively unusable as an
"intelligent" assistant. The product decision: Mjölnir should be intelligent and
have personality **by default**, with a local LLM as its brain. That brain is
Mjölnir's identity — Ollama today, swappable later for a differently-named
backend — so it must be a single, linked thing rather than two independent knobs.

Two Ollama configs existed independently: the router read inline
`llm_router_base_url`/`llm_router_model`, while the rephraser (a `summarizer`
plugin) read `engines["ollama"]`. They only coincided by default.

## Decision
1. **Default the LLM on.** `intent_router="llm"` and `persona_rephraser="ollama"`
   are the defaults. The deterministic grammar still runs first and offline
   (ADR-0012 deterministic-first, fail-closed), so a missing/slow Ollama only
   costs the *understanding* upgrade, never basic commands. A non-fatal startup
   probe warns (`mjolnir.llm_unreachable`) when the brain is expected but down.

2. **One canonical LLM identity.** New `llm_base_url` / `llm_model`
   (default `http://localhost:11434` / `llama3.1:8b`) are the single source both
   features derive from. The router reads them directly; the rephraser is linked
   in the composition root (`main.py` `_link_llm_identity`) by folding the
   identity into `engines[persona_rephraser]` — **keyed on the plugin name, not
   the literal "ollama"**, so renaming the backend (e.g. to "stormbreaker") is
   pure config and `plugins.py` stays generic (per the CLAUDE.md rule that
   backend-specific wiring lives only in the composition root). An explicit
   `MJOLNIR_ENGINES` entry still overrides. The old `llm_router_base_url`/`_model`
   fields are removed (the feature was opt-in and unreleased).

3. **Make the backend a real dependency.** `prodeo-summarizer-ollama` is now a
   dependency of `prodeo-mjolnir`, so the personality works with zero setup. It
   is lightweight (`prodeo` + `httpx`). Swappability is preserved by the config
   in (2), not by keeping the package optional.

4. **Widen the allowlist to include actions.** `llm_intents` now defaults to
   include `approve`/`deny`/`stop`. This **amends ADR-0012 property 4** (which
   kept actions off). The rest of ADR-0012's envelope is deliberately retained:
   deterministic-first ordering (most action phrasings never reach the LLM), the
   closed enum + allowlist filter, fail-closed on any error, and — critically —
   **the LLM still only supplies an intent + target hint; the handler resolves
   the real target against live cache data and keeps the single-match ambiguity
   guard, and confirmations stay deterministic templates.** So the worst a
   misclassified action can do is act on an unambiguous, live, named target — or,
   when ambiguous, ask instead of guessing.

5. **Use CUDA automatically.** The STT/TTS engines auto-detect GPU with no user
   toggle: faster-whisper defaults `device`/`compute_type` to `auto` (CUDA +
   `float16` when CTranslate2 sees a device, else CPU + `int8`), and Piper
   enables the CUDA execution provider only when onnxruntime actually exposes it.
   Both fall back to CPU (with a warning) if the GPU load fails. Ollama manages
   its own GPU. This keeps the "local-first, works on CPU" guarantee while using
   a GPU when present.

## Consequences
- Mjölnir is intelligent and in-persona by default against one local Ollama; a
  single knob (`MJOLNIR_LLM_MODEL` / `_BASE_URL`) repoints both features.
- Larger default model (`llama3.1:8b`) needs real VRAM / is slow CPU-only;
  overridable, and the briefing rephrase is bounded (`rephrase_timeout_s`) with
  deterministic fallback, the router fails closed on timeout.
- Voice can now trigger session actions via free-form phrasing, a deliberate
  capability increase bounded by the mitigations in Decision 4.
- GPU acceleration is free when available and invisible when not.

## Alternatives Considered
- **Keep it opt-in, just document it.** Rejected: the default experience is the
  product, and "dumb unless configured" is the problem being solved.
- **Two separate LLM configs.** Rejected: fails the "single, swappable identity"
  requirement; changing the model would mean editing two places.
- **Special-case "ollama" in the plugin loader.** Rejected: violates the
  no-backend-specific-logic-outside-the-composition-root rule.
- **Keep LLM actions off.** Considered (ADR-0012's posture); overridden by the
  product call that natural-language actions are core to feeling intelligent,
  given the handler-side guards make it safe.
- **A manual GPU flag.** Rejected: the user explicitly wanted no manual toggle;
  auto-detect with CPU fallback is strictly friendlier.
