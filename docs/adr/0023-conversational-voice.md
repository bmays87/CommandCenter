# ADR-0023: Conversational voice — follow-up listening, clarification dialogs, and confirm-first launches

- **Status**: Accepted
- **Date**: 2026-08-11
- **Extends**: ADR-0010 (client-hosted engines), ADR-0012 (LLM intent
  router), ADR-0013 (Ollama brain), ADR-0018 (grounded QA), ADR-0022
  (structured questions)

## Context

Mjölnir was strictly wake-word-gated and half-duplex: after it spoke
*anything* — including "claude-code on db asks: may I run the migration?" —
it dropped straight back to wake-only mode. Answering required the full
"hey Mjölnir" → "yes?" → "approve" dance. Ambiguity was a dead end ("2
sessions match api; say the project name" — then silence), and there was no
way to start a session by voice at all, though `POST /api/sessions` has
existed since Phase 2.

## Decision

1. **A bounded no-wake follow-up window, owned by the pipeline.** After
   speaking something that invites a reply, the next utterance is captured
   without the wake word for `followup_window_s` (8 s): no ack, same
   exchange correlation (a correlation chain may therefore begin at
   `voice.command_received`). The **echo-cooldown check stays ahead of the
   follow-up check and the window anchors after `mute_until`** — pinned by
   test — so TTS bleed can never become "the reply". Silence in the window
   says nothing: a declined invitation is not an error. No new event types.
2. **Handlers return a `Reply {text, expect_reply}` and own dialog state.**
   The pipeline owns mic modes; conversation semantics live in the handlers
   as a TTL-bounded dialog slot (confirm / slot-filling / choose) consulted
   before intent routing. `resume()` may decline (return `None`), handing
   the utterance back to normal routing — how a non-yes confirmation answer
   and a contradicting choose-reply avoid mis-execution. Cancel phrases
   always clear the dialog.
3. **Voice launch is slot-filled and confirm-first (hard rule).** Grammar
   and LLM router both *name* a `launch` intent with project/task hints —
   naming is safe on the allowlist for the same reason approve/deny were
   (ADR-0013): **a launch executes only on an explicit spoken yes to a
   read-back**, inside one method every launch path funnels through.
   Projects resolve against session history only (a filesystem path cannot
   be dictated; the template points new projects at the dashboard). The
   adapter comes from `GET /api/adapters` filtered to `capabilities.launch`
   (config override `MJOLNIR_LAUNCH_ADAPTER`; several → ask). Mjölnir stays
   agent-agnostic — no adapter name appears in the package.
4. **Clarify instead of dead-ending.** Ambiguous approve/deny/respond/stop
   targets open a choose dialog: candidates read with ordinals, the reply
   picks by position or name. Ordinals bind to the dialog's own candidate
   list, never `_last_pending` (pinned by test), and a contradicting action
   verb re-routes rather than executes.
5. **Announced interactions become answerable.** The notifier records which
   interaction it read out and opens the window. Routing is normal-first;
   only an `UnknownIntent` utterance is matched against the question's
   options (exact / unique containment / ordinal — generic label matching,
   posted as plain `text`, never adapter-native structure), falling through
   to the AnswerEngine on no match — ADR-0018's talker/actor boundary and
   ordering are preserved. Multi-part/multi-select → `needs_dashboard`
   (ADR-0022).

## Consequences

- The "hey Mjölnir → yes → approve" dance is gone for every announced
  request; conversations flow ("Which one?" — "two" — "Approved").
- Sessions can be started by voice with STT errors caught at the read-back,
  and never accidentally: three test-pinned invariants (no launch without
  yes; echo guard before follow-up; dialog ordinals never leak) carry the
  safety story.
- The follow-up window slightly widens the surface for stray speech to be
  interpreted; bounded by the short window, the mute-first rule, and the
  contradiction/ordinal guards.
- Half-duplex remains: Mjölnir still cannot be interrupted mid-speech
  (barge-in is future work and needs acoustic echo cancellation).

## Alternatives Considered

- **Always-on open mic after any speech** — turns every remark in the room
  into a command; the invitation-scoped window is the deliberate middle.
- **Launch without confirmation for "clearly parsed" requests** — a
  misheard project or prompt starts a real agent doing real work; the
  read-back costs one exchange and catches exactly that.
- **LLM-driven dialog state** — the clarification flows are small state
  machines with deterministic latency and offline guarantees; an LLM adds
  failure modes without adding capability here (the LLM keeps its ADR-0012
  classifier role).
- **Free-path voice launches** — dictating `F:\SourceCode\...` by voice is
  hopeless with real STT; session history is the natural, already-observed
  project vocabulary.
