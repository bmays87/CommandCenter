# Mediation Service

The Mediation Service (`prodeo.mediation`) owns the lifecycle of `Interaction`
objects: permission requests and questions from agents. It publishes
`interaction.*` events (see event-model.md), tracks timeouts, accepts exactly
one resolution per interaction, and routes the answer back to the blocked
agent.

## Lifecycle

```
            ┌──────────► answered   (first answer wins; delivered to the agent)
 pending ───┼──────────► timed_out  (permission: auto-denied; question: expires)
            └──────────► cancelled  (adapter withdrew it, or orphaned by restart)
```

An interaction opens when an adapter reports an `InteractionObservation`
(the Adapter Manager validates it and calls `MediationService.open`). It
resolves exactly once; every resolution is a published fact.

## External interactions (ADR-0011)

The second entry path: an externally blocked requester — the interactive
Claude Code `PermissionRequest` hook — submits its own interaction via
`POST /api/interactions/external` and long-polls for the resolution. The
Adapter Manager's `open_external_interaction()` differs from the
observation path in three deliberate ways:

- **No capability gate.** The requester carries the answer back itself, so
  `adapter.respond()` is never invoked and an observe-only adapter is fine.
- **The response distinguishes `answered` from everything else.** Only an
  explicit human answer carries an `answer` object; `timed_out` and
  `cancelled` return `answer: null` and the requester falls through to its
  own local prompt (never an implicit deny).
- **A timed-out permission leaves the session `waiting_on_user`.** Unlike the
  adapter path (where timeout auto-denies and the agent moves on), the
  external requester reacts to timeout by prompting its human locally — the
  session is still honestly parked on a person, and transcript activity
  resumes it naturally once they answer.
- **Discovery never un-parks a session.** Re-discovery's file-level state
  hints (mtime heuristics) cannot see a mediation block, so the registry
  ignores them entirely while a session is `waiting_on_user`; only mediation
  resolution or the watcher's explicit observations resume it.

If the requester disconnects mid-poll (its human answered at the terminal, or
the hook process died), the route withdraws the pending interaction with
reason `requester_disconnected` — the card leaves the inbox instead of going
stale. ADR-0007's restart semantics apply unchanged: a pending external
interaction does not survive a server restart, and the hook's fail-open
posture shows the terminal prompt instead.

## Design decisions

- **Exactly-once resolution.** `answer()` flips status synchronously — no
  `await` between the pending check and the flip — which is atomic on the
  single event loop. Concurrent second answers raise
  `InteractionAlreadyResolvedError` (API: 409).
- **The answer is the fact.** `interaction.answered` publishes *before*
  delivery to the adapter is attempted. A delivery failure is reported as an
  `adapter.error` (and the session may remain `waiting_on_user`) but never
  rolls back the human's decision.
- **Answer routing without service coupling.** Mediation never imports the
  Adapter Manager. `open()` takes a per-interaction `deliver(interaction, answer)` callback;
  the manager supplies a closure over the owning adapter's `respond()`. This
  keeps the dependency one-directional (manager → mediation), wired only in
  `server.py`.
- **Pending interactions do not survive restarts** (ADR-0007). Deliver
  callbacks are process-local, so `rebuild()` folds the `interaction.*` log for
  history and cancels anything still pending with reason
  `orphaned_by_restart`. Boot order matters: store → recorder → registry
  rebuild → mediation rebuild, so the cancellations are recorded.
- **Timeouts auto-deny permissions.** Per-interaction `timeout_s` (adapter
  supplied) falls back to `PRODEO_MEDIATION_DEFAULT_TIMEOUT_S`; unset means
  wait forever. On timeout a permission delivers `deny "timed out"` so the
  blocked agent proceeds safely.
- **Session state coupling.** The Adapter Manager (not mediation) transitions
  the session `running → waiting_on_user` when an interaction opens and back
  after a successful `respond()`.

## Known limitation

If the adapter fails while delivering an answered interaction, the interaction
is answered but the agent stays blocked; this surfaces as `adapter.error` with
the session stuck in `waiting_on_user`. Accepted for Phase 2 — the agent-side
timeout (e.g. a hook timeout) eventually unblocks the agent.
