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
| `system` | `started`, `stopping`, `plugin_loaded`, `plugin_failed` |
| `voice` *(phase 4)* | `wake_word_detected`, `command_received`, `transcription_completed`, `speech_started`, `speech_finished` |

The names in the project brief map as follows: `AgentStarted` → `session.started`,
`PermissionRequested` → `interaction.requested` (kind=permission), `QuestionAsked` →
`interaction.requested` (kind=question), `ToolStarted` → `tool.started`, etc.
Permission requests and agent questions share one mechanism deliberately: both are
"the agent is blocked on a human," differing only in answer type.

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

## Bus Semantics (v1, in-process)

- Async pub/sub with per-subscriber queues; a slow subscriber never blocks the bus.
- Delivery is at-least-once to the persistence subscriber (which writes before the
  event is visible to query APIs) and best-effort to live UI streams — clients
  reconcile via the query API on reconnect, using the last seen ULID as a cursor.
- Wildcard subscriptions (`session.*`, `*`) are supported.
- Backpressure policy per subscriber: `block`, `drop_oldest`, or `disconnect`
  (used for misbehaving WebSocket clients).

See ADR-0002 for why the bus is in-process in v1 and how it scales out later.
