# ADR-0022: Structured multi-part agent questions

- **Status**: Accepted
- **Date**: 2026-08-11
- **Extends**: ADR-0019 (question answering); amends its §1 "v1 limitation"

## Context

ADR-0019 made single-question, single-select `AskUserQuestion` calls
answerable from the dashboard, but deliberately let multi-question and
multi-select calls fall back to the permission presentation: a raw JSON dump
with Approve/Deny buttons. The user hit exactly that — a question rendered
as an unreadable JSON string with buttons that cannot answer it (approving
runs the tool with *no* answers).

The dashboard already renders question-kind interactions correctly; the gap
was that flat `options: list[str]` cannot express "three questions, one of
them multi-select". The fix needs structure — but the structure must stay
agent-agnostic in core (no `AskUserQuestion` shapes in `src/prodeo`).

## Decision

1. **Generic structured questions in core.** `prodeo.mediation.model` gains
   `QuestionOption {label, description}` and `QuestionGroup {id, prompt,
   options, multi_select}`; `InteractionRequest`/`Interaction` gain
   `questions: list[QuestionGroup]` and `Answer` gains
   `selections: dict[str, list[str]]` (group id → chosen labels). All fields
   default — `interaction.requested` stays payload v1, old stored events
   rebuild with `questions=[]`, and the dashboard falls back to the flat
   rendering for them. No upcast needed.
2. **The dashboard posts generic `selections`, never `updated_input`.**
   ADR-0019's rule stands: only the adapter that opened the interaction maps
   selections to its agent's native input. The dashboard renders one
   fieldset per group (radios or checkboxes) with a single Submit, enabled
   once every group has a selection.
3. **The adapter classifies every well-formed `AskUserQuestion` as a
   question.** `format.py` builds `QuestionGroup`s for any 1..N well-formed
   questions (multi-select included); only genuinely malformed input keeps
   the permission fallback. Group ids are the exact question texts (the key
   Claude Code's `answers` map uses), with `" #2"` suffixes on duplicates
   (internal only; the un-suffixed text is recovered by index). Flat
   `options` stay populated only for the single-question single-select shape
   — preserving one-click and voice-ordinal answering exactly as today.
4. **`questions_updated_input(input, selections)`** maps selections to the
   `answers` contract: every question must match ≥1 offered label
   (single-select exactly one) or the whole mapping returns `None` — refuse
   to fabricate, never guess. The multi-select value joins the chosen labels
   with `", "` — **verified against real multiSelect transcripts** (the
   `answers` contract is Claude-Code-owned; `format.py` is its single
   follower). Both delivery paths honor it: the SDK bridge's `can_use_tool`
   and the hook's `decision_output` (whose external payload now carries
   `questions`). A selections mismatch denies on the SDK path (the agent
   must not run unanswered) and falls through to the terminal prompt on the
   hook path.
5. **Voice**: a multi-part or multi-select question cannot be answered with
   one utterance; Mjolnir says so (`needs_dashboard`) instead of posting
   text the adapter would refuse.

ADR-0019 §4 is untouched: SDK-launched sessions still never surface
`AskUserQuestion`; this fixes presentation and answering, primarily
benefiting the hook path (interactive sessions), and is already correct on
the SDK path if the SDK ever surfaces the tool.

## Consequences

- Multi-part and multi-select questions render as real forms and are
  answerable from the dashboard; the raw-JSON-with-Approve/Deny card is gone
  for every well-formed question.
- Core stays generic: `questions`/`selections` carry no Claude specifics;
  other adapters can reuse them as-is.
- The `", "` join is a bet on an undocumented (but transcript-verified)
  contract; if Claude Code changes it, `format.py` is the single place to
  follow, and the refuse-to-fabricate rule means a mismatch degrades to the
  terminal prompt, never to a wrong answer.
- Labels containing ", " are ambiguous *to parse* but not to generate — we
  only ever generate, so the join stays safe on our side.

## Alternatives Considered

- **Dashboard builds `updatedInput` directly** — rejected by ADR-0019
  already; would smuggle the Claude-Code answers contract into the browser.
- **Serialized single-question flow (ask parts one at a time)** — cannot
  work: the agent blocks on one tool call carrying all questions; there is
  nothing to answer piecemeal.
- **Flat options with encoded group prefixes** (`"1: Option A"`) — pushes
  structure into strings the voice path would read aloud; strictly worse
  than typed groups.
