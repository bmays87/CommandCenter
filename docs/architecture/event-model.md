# Event Model

Events are the backbone of Command Center. Every state change in the system is an
event; persistence, the dashboard, notifications, and voice are all downstream
consumers of the same stream.

## Envelope

Every event shares a common envelope (Pydantic model, JSON on the wire):

```json
{
  "id": "01J9ZX7Q8B4N4C4T4YV9WZK3F2",        // ULID: sortable, unique
  "type": "session.state_changed",           // dot-namespaced, past tense
  "version": 1,                              // schema version of the payload
  "timestamp": "2026-07-11T14:03:22.418Z",   // UTC, always
  "node": "workstation-01",                  // originating machine (multi-node ready)
  "session_id": "cc-8f2a...",                // optional: owning session
  "correlation_id": "01J9ZX...",             // optional: causal chain
  "source": "adapter:claude-code",           // emitting component
  "payload": { ... }                         // type-specific, versioned schema
}
```

Rules:

- **Past tense, dot-namespaced types.** `session.started`, not `StartSession`.
- **Events are immutable facts.** Consumers may not mutate or reject them.
- **Additive evolution.** New optional payload fields do not bump `version`.
  Removing or repurposing a field requires a version bump and a documented
  upcast function so old stored events remain readable. Schemas live in
  `prodeo/events/` and are the single source of truth.
- **ULIDs, not UUIDs**, so event IDs sort chronologically for free.

## Core Taxonomy (v1)

| Namespace | Events |
|---|---|
| `session` | `discovered`, `started`, `state_changed`, `completed`, `failed`, `stopped`, `archived` |
| `agent` | `output_appended`, `turn_started`, `turn_completed` |
| `tool` | `started`, `finished`, `failed` |
| `interaction` | `requested`, `answered`, `timed_out`, `cancelled` |
| `adapter` | `loaded`, `unloaded`, `error`, `discovery_completed` |
| `notification` | `sent`, `failed` |
| `schedule` | `created`, `triggered`, `deleted` |
| `summary` *(phase 3)* | `generated` |
| `system` | `started`, `stopping`, `plugin_loaded`, `plugin_failed`, `retention_completed` |
| `voice` *(phase 4)* | `wake_word_detected`, `command_received`, `transcription_completed`, `speech_started`, `speech_finished` |

The names in the project brief map as follows: `AgentStarted` → `session.started`,
`PermissionRequested` → `interaction.requested` (kind=permission), `QuestionAsked` →
`interaction.requested` (kind=question), `ToolStarted` → `tool.started`, etc.
Permission requests and agent questions share one mechanism deliberately: both are
"the agent is blocked on a human," differing only in answer type.

## Interaction Events (Phase 2)

An `Interaction` is "the agent is blocked on a human." The Mediation Service is
the only writer of `interaction.*` events and accepts **exactly one resolution**
per interaction — first answer wins; later answers are rejected (HTTP 409 at the
API). Payloads (v1):

- `interaction.requested` — `{"interaction": {id, session_id, adapter, native_id,
  kind: "permission"|"question", title, body, options[], requested_at,
  timeout_at?, status, ...}}` (full Interaction dump; state is rebuilt from this
  plus the resolution events).
- `interaction.answered` — `{"interaction_id", "answer": {decision?:
  "allow"|"deny", text, updated_input?}, "answered_by"}`.
- `interaction.timed_out` — `{"interaction_id"}`. A timed-out **permission** is
  auto-denied toward the agent; a timed-out question simply expires.
- `interaction.cancelled` — `{"interaction_id", "reason"}`. Emitted when the
  adapter withdraws the interaction (e.g. it was answered in the terminal) or
  when a pending interaction is orphaned by a server restart (ADR-0007).

All interaction events carry the owning `session_id` in the envelope and
`source: "mediation"`.

## Notification Events (Phase 2)

The Notifier routes selected events to channels per `PRODEO_NOTIFY_RULES`
(pattern → channel names) and reports the outcome (`source: "notifier"`):

- `notification.sent` — `{"channel", "event_id", "title"}` (`event_id` is the
  event that triggered the notification).
- `notification.failed` — same fields plus `"error"`. Channel failures are
  contained: they produce this event, never an exception in the core.

`notification.*` events are themselves never routed (loop guard).

## Schedule Events (Phase 3)

The Scheduler is the only writer of `schedule.*` events (`source: "scheduler"`)
and rebuilds its catalogue from them on boot. Payloads (v1):

- `schedule.created` — `{"schedule": {id, name, cron, adapter, spec,
  created_at, ...}}` (full Schedule dump; `spec` is the LaunchSpec fired on
  each trigger).
- `schedule.deleted` — `{"schedule_id"}`.
- `schedule.triggered` — `{"schedule_id", "name", "adapter"}` plus either
  `"session_id"` (also in the envelope) when the launch succeeded or
  `"error"` when it was contained. Runs missed while the server was down are
  skipped, not backfilled — the next run is always computed from *now*.

Cron expressions are standard 5-field (plus `@daily`-style aliases), evaluated
in `PRODEO_SCHEDULER_TIMEZONE` (default: the server's local timezone).

## Summary Events (Phase 3)

The Summary Service publishes one `summary.generated` per scheduled digest run
(`source: "summary"`; cron via `PRODEO_SUMMARY_CRON`, default daily at 18:00):

- `summary.generated` — `{"period_start", "period_end", "stats": {counts},
  "digest": <plain-text statistics>, "prose": <summarizer text or "">,
  "summarizer": <plugin name or null>, "summarizer_error"?}`. The digest is
  always produced; prose appears only when a `summarizer` plugin (e.g.
  `prodeo-summarizer-ollama`) is installed, and its failures are contained
  into `summarizer_error`, never into a missing digest.

Route `summary.generated` to a notification channel to receive it.

## Retention (Phase 3)

Retention is opt-in via `PRODEO_RETENTION_RULES` (a list of
`{"types": <pattern>, "max_age_days": N, "archive": true}` rules). Expired
events are appended to monthly gzip JSONL archives
(`<data_dir>/archive/events-YYYY-MM.jsonl.gz`, plain event envelopes) before
deletion. Two safety rails:

- **Rebuild-critical namespaces are never deleted** — `session.*`,
  `schedule.*`, and `interaction.*` are how the registry, scheduler, and
  mediation reconstruct state on boot; retention skips them regardless of the
  rules. The log's bulk (`agent.*`, `tool.*`, `notification.*`) is what
  expires.
- Sessions finished longer than `PRODEO_RETENTION_ARCHIVE_SESSIONS_AFTER_DAYS`
  ago transition to `archived` through the normal state machine.

A pass that changed anything publishes `system.retention_completed` —
`{"events_deleted", "events_archived", "sessions_archived"}`
(`source: "retention"`).

## Session State Machine

```
 discovered ──► starting ──► running ◄──► waiting_on_user
                              │  ▲              │
                              │  └── resumed ◄──┘
                              ▼
                 completed │ failed │ stopped ──► archived
```

Adapters report raw observations; the Session Registry owns the canonical state
machine and rejects illegal transitions (emitting `adapter.error` when an adapter
misbehaves rather than corrupting state).

Two implementation notes (Phase 1):

- `session.state_changed` is emitted for **every** transition and is what state is
  rebuilt from. The specific lifecycle events (`session.started`, `.completed`,
  `.failed`, `.stopped`, `.archived`) are emitted *additionally* for semantic
  consumers such as notifications; they carry no state a fold needs.
- Terminal states other than `archived` may transition back to `running`
  ("resumed"). Observe-only adapters can see a session they classified as
  finished start appending output again; treating that as a resume is more
  truthful than rejecting the observation.

## Bus Semantics (v1, in-process)

- Async pub/sub with per-subscriber queues; a slow subscriber never blocks the bus.
- Delivery is at-least-once to the persistence subscriber (which writes before the
  event is visible to query APIs) and best-effort to live UI streams — clients
  reconcile via the query API on reconnect, using the last seen ULID as a cursor.
- Wildcard subscriptions (`session.*`, `*`) are supported.
- Backpressure policy per subscriber: `block`, `drop_oldest`, or `disconnect`
  (used for misbehaving WebSocket clients).

See ADR-0002 for why the bus is in-process in v1 and how it scales out later.
