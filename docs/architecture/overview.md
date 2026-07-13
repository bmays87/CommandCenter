# Architecture Overview

## System Shape

Command Center is a single headless daemon (`prodeo-server`) composed of loosely
coupled internal services that communicate exclusively through an event bus, plus
any number of external clients that consume its API.

```
┌────────────────────────────  Clients  ────────────────────────────┐
│  Web Dashboard   Voice Client   Mobile   CLI   Automation/Webhooks │
└──────────────┬───────────────────────────────────────┬────────────┘
               │ REST (commands, queries)              │ WebSocket / SSE (event stream)
┌──────────────▼───────────────────────────────────────▼────────────┐
│                        API Layer (FastAPI)                         │
├────────────────────────────────────────────────────────────────────┤
│                          Event Bus (async)                         │
├──────────┬──────────┬───────────┬───────────┬───────────┬─────────┤
│ Session  │ Adapter  │ Mediation │ Scheduler │ Notifier  │ Plugin  │
│ Registry │ Manager  │ Service   │ Service   │ Service   │ Host    │
├──────────┴─────┬────┴───────────┴───────────┴───────────┴─────────┤
│                │        Persistence (EventStore + StateStore)      │
├────────────────▼───────────────────────────────────────────────────┤
│   Agent Adapters (plugins):  claude-code │ codex │ aider │ ...     │
└────────────────────────────────────────────────────────────────────┘
```

## Core Services

**Session Registry** — the authoritative in-memory + persisted catalogue of every
known agent session (active or historical), keyed by a Command-Center-assigned
`session_id`. Built entirely from events; it holds no adapter-specific logic.

**Adapter Manager** — loads adapter plugins, invokes their discovery routines,
supervises their watch tasks, and translates adapter callbacks into domain events.
Adapters never touch the bus directly; they report through a narrow `AdapterContext`
handed to them, which keeps the adapter API surface small and auditable.

**Mediation Service** — owns the lifecycle of `Interaction` objects (permission
requests, questions from agents). It publishes `interaction.requested` events, tracks
timeouts, accepts exactly one resolution (first client wins), and routes the answer
back through the owning adapter.

**Scheduler Service** — cron-like launching of agent runs. Deliberately minimal in
early phases; behind a `Scheduler` interface so it can be replaced.

**Notifier Service** — fans out selected events to notification channel plugins
(desktop, ntfy, email, Home Assistant, ...) according to user routing rules.

**Plugin Host** — discovers, loads, configures, and health-checks plugins of every
kind (adapters, notification channels, STT/TTS engines, storage backends).

## Communication Rules

1. Services never import each other. They publish and subscribe to events, and expose
   query interfaces registered with a lightweight service container (constructor
   dependency injection — no magic framework).
2. Commands flow **inward** (client → API → service). Facts flow **outward**
   (service → bus → persistence → clients). A command may be rejected; an event is
   history and may not be.
3. Anything a UI can display must be derivable from the event stream plus the query
   API. This is what keeps the core headless and clients thin.

## Process Model

- The server is a single asyncio process. CPU-heavy or crash-prone work (audio
  inference, subprocess-wrangling adapters) runs in subprocesses or executors.
- The voice client is a **separate process** (potentially a separate machine) that
  talks to the server over the same WebSocket/REST API as any other client. Audio
  never enters the core.

## Deferred by Design (with seams in place)

- **Multi-machine**: the event bus is behind an `EventBus` interface; a future
  NATS/Redis-backed implementation plus a node identity field on every event enables
  a hub-and-agents topology without reworking services. See ADR-0002.
- **Multi-user**: every API route already passes through an auth dependency; v1 ships
  a single-token implementation.
