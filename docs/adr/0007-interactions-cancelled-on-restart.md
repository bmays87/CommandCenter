# ADR-0007: Pending interactions are cancelled on server restart

- **Status**: Accepted
- **Date**: 2026-07-15

## Context
Interactions are event-sourced (`interaction.*`), so the Mediation Service can
rebuild its catalogue from the log on boot. But the *delivery path* for an
answer — the adapter-side pending future (an SDK `can_use_tool` await, a
blocked hook HTTP request) — is process state that dies with the daemon. A
rebuilt "pending" interaction would render in the inbox as answerable while no
agent is listening for the answer.

## Decision
`MediationService.rebuild()` folds the log for history, then marks anything
still pending as CANCELLED with reason `orphaned_by_restart`, publishing
`interaction.cancelled` through the bus (after the recorder is running, so the
resolution is durable). If the agent is genuinely still blocked, the adapter
re-reports the interaction and a fresh one opens with a live delivery path.

## Consequences
The inbox never shows unanswerable zombies; the log tells the truth about what
happened. Cost: an agent blocked across a daemon restart must re-surface its
request (adapters that watch live sessions do this naturally on re-attach).

## Alternatives Considered
**Resurrect as pending** — lies to the user until an answer disappears into
nothing. **Drop from history** — violates "events are immutable facts."
