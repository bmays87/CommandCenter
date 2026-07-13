# ADR-0002: In-process event bus in v1, behind an interface

- **Status**: Accepted
- **Date**: 2026-07-11

## Context
Everything communicates via events, and the long-term vision includes multi-machine
orchestration. Deploying NATS/Redis/Kafka from day one contradicts "one process,
local first, operationally simple."

## Decision
Define a small `EventBus` interface (publish, subscribe-with-pattern, per-subscriber
queues/backpressure). v1 ships only an asyncio in-process implementation. Every event
carries a `node` field from day one. Persistence subscribes like any other consumer
but is written before events become queryable.

## Consequences
Zero infrastructure for the common case; the seam for a NATS-backed implementation
exists (Phase 5) without touching services. Risks accepted: in-process delivery
semantics (at-least-once to persistence, best-effort to live clients) must be
documented so client authors build reconciliation (ULID cursors) now — retrofitting
that discipline later is much harder than starting with it.

## Alternatives Considered
**External broker from day one**: kills single-process install. **No abstraction,
refactor later**: the interface is cheap now and extremely expensive later, and this
is one of the few places where speculative abstraction is justified by an explicit
roadmap item.
