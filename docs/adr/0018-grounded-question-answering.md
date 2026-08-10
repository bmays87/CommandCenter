# ADR-0018: Mjölnir answers questions, grounded in a frame of reference

- **Status**: Accepted
- **Date**: 2026-08-10
- **Extends**: ADR-0012 (constrained LLM router), ADR-0013 (Ollama brain)

## Context

Live testing after Phase 5: Mjölnir hears correctly, routes commands correctly,
and is still "dumb" — ask it "what is an agent?", "what's your purpose?", or
"why is it asking me that?" and it says *"Sorry, I didn't understand"*. The
architecture explains why: both routers are **classifiers over a closed intent
enum**. Nine intents exist; anything else is `UnknownIntent`, and its handler
is a template. The system had speech recognition and command routing, but no
ability to answer a question at all, and its LLM prompts carried no identity,
no domain vocabulary, and no state.

The user's requirement, verbatim in spirit: it should know what agents are and
what its own purpose is, and it should have a frame of reference so context is
automatically present.

## Decision

A third stage behind the `Router` seam's failure path: when the grammar *and*
the LLM classifier both produce `UnknownIntent`, the utterance is handed to an
`AnswerEngine` as a **question**, not a failed command.

### 1. The frame of reference is the system prompt

Every call carries: who Mjölnir is and what it is for; a domain glossary
(agents, sessions, adapters, permission requests, the dashboard); the spoken
command vocabulary, so it can tell the user the phrase that performs an action;
and grounding rules — live facts come only from the state block, unknown means
say so, answers are one to three plain speakable sentences.

### 2. Context is automatic, from the cache that already exists

The engine renders a state snapshot from the same `LocalCache` every query
handler reads: active sessions by speakable name with adapter, state, project,
and recency; pending permissions with the same ordinals the `pending` readout
uses (so an answer can sensibly say "that's number two"); sessions finished in
the last twelve hours. Empty states are stated ("Active sessions: none."),
because an omitted fact is a fact the model will invent. No new server calls,
no added latency beyond the LLM call itself.

### 3. A talker, never an actor

ADR-0012's safety envelope is untouched. The engine produces speech and
nothing else; it cannot resolve an interaction, name an id to anything, or
widen the intent set. When the user asks it to *do* something, its instructed
move is to say the command phrase. Actions still flow exclusively through the
closed enum, the allowlist, and the handlers' ambiguity guards.

### 4. Fail closed to exactly the old behaviour

Engine off (`question_answering="off"`), Ollama unreachable, timeout
(`qa_timeout_s`, default 10 s), or an empty reply: the handler falls back to
the deterministic "didn't understand" template. Offline, Mjölnir behaves
precisely as it did before this ADR.

### 5. One brain, one identity

The engine reads `llm_base_url` / `llm_model` directly — the single identity
ADR-0013 established. The honorific is passed through so the persona's address
survives into free-form answers.

## Consequences

- "What are agents?", "what can you do?", "what's going on?" phrased any way
  the grammar misses — answered, with the live picture already in context.
- An unmatched utterance now costs up to two LLM calls (classifier, then
  answer). Known commands still pay zero; the deterministic grammar remains
  first. Accepted: the alternative was a merged classify-or-answer call, and
  keeping the classifier's frozen-enum contract untouched was worth a second
  call on the rare path.
- Answer quality is bounded by the local model. The grounding rules constrain
  invention but cannot eliminate it; the state block keeps the facts it can
  reach for accurate.
- The frame of reference is prose in `answers.py`. If the command vocabulary
  changes, that text must change with it — same discipline as the help
  template, and the test pins the coupling.

## Alternatives Considered

- **Widen the intent enum with a `question` intent the classifier emits.**
  Rejected: identical outcome, but it entangles the QA path with the frozen
  enum that exists as a safety boundary. Unknown-means-question keeps the
  classifier's contract byte-for-byte.
- **One merged call that classifies or answers.** Rejected: the classifier's
  JSON-only, closed-enum output format is load-bearing (ADR-0012 §1); mixing
  free prose into the same response weakens exactly the property that makes
  misuse hard.
- **Let the engine call server APIs (tools) to fetch what it needs.**
  Rejected: that is an executor. The cache already holds what voice questions
  need; read-only snapshot injection gets the benefit with none of the surface.
- **Template-only "I am Mjölnir…" canned answers.** Rejected: it answers two
  questions and fails the third; the requirement was understanding, not a
  longer help text.
