# ADR-0019: Agent questions are answerable from the dashboard

- **Status**: Accepted
- **Date**: 2026-08-10
- **Extends**: ADR-0011 (permission-hook mediation), ADR-0008 (SDK control)

## Context

Claude Code's `AskUserQuestion` — a multiple-choice question with labeled,
described options, one conventionally marked "(Recommended)" in its label —
reached Command Center as a generic permission: title "Allow AskUserQuestion?",
body a raw JSON dump, controls Approve/Deny. The options were never captured,
and approving let the tool run *with no answers*, which is worse than useless.
The user could see that an agent was waiting but could not actually answer it
from the web. This blocked the stated goal of working directly from Command
Center, and blocks the planned voice flow, which must read and answer the same
interaction.

Everything below the presentation already existed: `Interaction` carries
`kind="question"`, `body`, and `options: list[str]` end to end; `Answer`
carries `text` and `updated_input`; the dashboard renders option buttons for
question-kind and posts the label as `text`; both delivery paths already
forward `updated_input` to the agent. What was missing was the mapping at the
two ends — and it is Claude-Code-specific, so it belongs in the adapter, not
core.

## Decision

### 1. `AskUserQuestion` becomes a question-kind interaction

`format.py` (shared by the hook and the SDK bridge, so both paths present
identically) gains `interaction_content(tool_name, input)`: a single-select,
single-question `AskUserQuestion` maps to kind `question`, title = the
question text, options = the labels verbatim ("(Recommended)" suffix and all),
and a body that lists each option as "N. label — description". The body is
written to be read aloud as much as rendered: it is the voice readout later.

Multi-question and multi-select calls keep the permission presentation — one
option row cannot answer two questions, and a single label cannot express a
multi-selection. A v1 limitation, deliberately.

### 2. The chosen label maps back through `updatedInput`

The contract: the agent reads selections from `answers`, a map of question
text to chosen option label, merged into the original tool input.
`question_updated_input(input, chosen)` builds it, matching the human's reply
as an exact label first, then case-insensitively, then positionally
("option 2", "number 2", "2") — the shapes a click, a typed reply, and a
spoken answer actually take. **No match maps to nothing**: the hook falls
through to the terminal prompt, the SDK bridge denies with the unmatched text
in the message. Fabricating a choice the human did not make is the one
prohibited outcome.

### 3. Both delivery paths, same mapping

- **Hook** (interactive sessions — the CLI in a terminal or VS Code): submits
  kind/title/body/options from `interaction_content`; a question-kind
  resolution answered with bare text becomes allow + `updatedInput`. This is
  the path that fires for a user's real agent sessions, and it is verified
  end to end: a real `AskUserQuestion` PermissionRequest becomes a
  question-kind interaction with options, and "option 2" resolves to the right
  label in `updatedInput`.
- **SDK bridge** (CC-launched sessions): `_on_sdk_interaction` creates the
  same interaction and `can_use_tool` maps a text answer identically — **when
  the tool routes through `can_use_tool` at all.**

Core is untouched — the mediation service already accepted text answers, and
the API models already carried every field.

### 4. Known limitation: AskUserQuestion on SDK-launched sessions

`can_use_tool` is the SDK's *permission* callback. Live testing showed that a
headless SDK session's `AskUserQuestion` does **not** raise a permission
control-request, so the bridge is never invoked and the tool has no UI to
answer it — the session parks on the tool call. Permission-kind tools
(Bash, Write, …) on SDK sessions mediate exactly as before; only the
interactive question tool is affected, and only on the launch path.

The mapping is kept in `can_use_tool` regardless: it is correct if the SDK
ever surfaces the tool this way, and harmless otherwise. Until then, sessions
started *from Command Center* should avoid interactive question tools — the
New Session form says so — while questions from interactive sessions (the
common case) are fully answerable through the hook. Closing the SDK gap is a
follow-up (likely a permission-mode or tool-config change that forces the
question tool through the permission path), not a blocker for the feature.

## Consequences

- An agent's question shows in the Inbox with its full text and options; one
  click answers it and the agent resumes with the selection. The resolved row
  now shows the chosen label (it previously displayed only decisions).
- The presence gate (ADR-0011) still applies: at the keyboard, prompts go to
  the terminal, not the web. `PRODEO_PRESENT_THRESHOLD_S=0` opts a machine
  into Command-Center-first mediation — every prompt goes to CC and the
  terminal prompt appears only on CC timeout. Documented in
  running-the-system.md; not a default change.
- The `updatedInput.answers` contract is asserted by tests but defined by
  Claude Code; if it shifts, the mapping functions in `format.py` are the
  single place to follow it.
- Free-text answers to a question remain legal and unvalidated in core;
  only the adapter decides whether text names an option.

## Addendum (2026-08-10): permission mode and session cleanup

Two adjacent controls landed with the same "work from Command Center" goal:

- **Live permission mode.** `set_permission_mode` joins the control surface
  (adapter API v4), mirroring `set_model` exactly: capability flag → adapter →
  launcher → the SDK's `set_permission_mode`. Four modes, human-labelled in the
  dashboard, adapter-native on the wire — Manual (`default`), Plan (`plan`),
  Edit Automatically (`acceptEdits`), Auto (`bypassPermissions`). The API
  validates the mode against that closed set before any adapter sees it. A
  launch sets the starting mode via `LaunchRequest`; the SessionView picker
  switches it live on controllable sessions. `Session.permission_mode` is
  descriptive, refreshed on the control call like `model`.

- **Archive, not delete.** `POST /api/sessions/{id}/archive` transitions a
  terminal session to `ARCHIVED` — event-sourced (ADR-0002), so it survives a
  rebuild and the history is never destroyed. Only terminal sessions archive
  (a running one is 409 "stop it first"). The fleet hides archived sessions;
  that is the "clean up" the user wanted, without a hard delete fighting the
  log-as-truth model.

## Alternatives Considered

- **Structured option objects (label/description/recommended) in core.**
  Rejected for now: `list[str]` labels plus a descriptive body covers click
  and voice; a richer model would touch core, API, and codegen for
  presentation-only gain.
- **Mapping the selection in the dashboard** (posting `updated_input`
  directly). Rejected: the browser would need to know an agent-specific SDK
  contract; core and clients stay generic, adapters translate.
- **Answering with `decision=allow` + option index.** Rejected: question-kind
  answers are text everywhere already (external API, dashboard, future voice);
  one shape for one meaning.
