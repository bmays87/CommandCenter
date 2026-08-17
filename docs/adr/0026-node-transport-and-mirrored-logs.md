# ADR-0026: Node transport — mirrored logs over pinned HTTPS, no broker

- **Status**: Accepted
- **Date**: 2026-08-17
- **Extends**: ADR-0020 (the split), ADR-0024 (machine registry),
  ADR-0025 (pairing and trust)

## Context

With machines paired (ADR-0025), Phase 6 needs the actual plumbing: events
from a CCAN's agents must reach the hub's log and dashboards, and hub
commands ("launch here", "terminate", an inbox answer) must reach the CCAN
that owns the session. The roadmap left the transport open: `EventBus` over
NATS or Redis Streams, "ADR at the time". This is that ADR.

## Decision

### 1. No broker. The CCAN keeps its own log; the hub mirrors it.

Each CCAN runs the same local stack the colocated server always ran — bus,
SQLite event log, session registry, mediation, adapter manager with
entry-point-discovered adapters (`prodeo_ccan.node.NodeHost`) — and its
`EventBus` stays in-process (ADR-0002 holds). Distribution happens at the
edge: the hub's **NodeSync** long-polls each paired machine's
`GET /ccan/v1/events` with a per-node ULID cursor and republishes the
events verbatim onto the hub bus, where the recorder persists them (the
store is idempotent per event id) and the WebSocket pushes them to
dashboards.

NATS/Redis were rejected for now: they add an operated dependency to a
system whose core dependency list is deliberately short, for event volumes
(one hub, a handful of machines) that plain HTTPS handles comfortably. The
`EventBus` Protocol keeps the door open if fleet sizes ever demand it.

The **cursor is derived, not stored**: the newest event the hub already
holds for that node (`EventQuery(node=...)`). A newly paired machine
therefore syncs from the beginning of its log, in the CCAN's own
(locally monotonic) ULID order — which is what makes the live folds safe:
no fact ever arrives before its antecedents.

### 2. Read-models fold mirrored facts; writers stay put

`SessionRegistry.apply_remote` / `MediationService.apply_remote` fold
mirrored `session.*` / `interaction.*` facts live, exactly as rebuild does
— and *only* for events from other nodes. The owning CCAN remains the sole
**writer** for its sessions and interactions; the hub holds read-models.
Boot-time folding comes free: rebuild already pages every node's events
from the hub store. Three guards keep the writer rule honest:

- `Interaction` gained `node` (additive), so the hub's restart orphan-sweep
  cancels only *its own* pendings — a remote machine's mediation is alive
  and still waiting.
- Retention's auto-archive skips sessions owned by other nodes.
- NodeSync drops any event whose `node` isn't the machine it's syncing —
  a CCAN speaks only for itself.

The hub runs the only Notifier, so mirrored `interaction.requested` events
notify exactly once, and the daily summary naturally becomes fleet-wide.

### 3. Commands route by ownership, over the pinned channel

Hub API handlers check `Session.node` / `Interaction.node`: local →
`AdapterManager`/`MediationService` as ever; remote → **NodeGateway**
forwards to the owning CCAN's `/ccan/v1/*` command plane (launch,
terminate, prompt, model, permission-mode, interrupt, context, answer),
translating its status/detail back verbatim (`RemoteNodeError`).
`LaunchRequest.machine_id` picks the machine; the new-session form offers
the *selected tab's* adapters (`GET /api/adapters?machine=`).

Every post-pairing call is **pinned**: `pinned_client` presents the hub
certificate and accepts exactly the certificate recorded at pairing —
closing the trust-on-first-use window ADR-0025 documented. Direction stays
hub → node for everything; the outbound-only alternative (ADR-0011's hook
shape) was not needed — pairing already required inbound reachability, and
one direction keeps the trust story single-shaped. Interactive sessions'
permission hooks keep talking straight to the hub API as before, which
works unchanged for remote machines (the mirrored session resolves there).

### 4. Known limits, accepted deliberately

- **Answer latency over resolution correctness**: a hub-answered remote
  interaction resolves on the CCAN and the fact mirrors back within a
  poll; the inbox may show "pending" for that beat.
- **Cross-node ULID interleaving**: mirrored ids sort by the *remote*
  clock, so a WS client's `after` catch-up can skip late-arriving remote
  history. Dashboard correctness rides the query APIs (invalidate →
  refetch), not stream completeness; a broker or arrival-order cursor
  fixes this if it ever matters.
- **`(adapter, native_id)` is not node-scoped** in the registries' native
  indexes; native ids are UUIDs, so collisions are negligible — noted, not
  defended.
- The installer now bundles the claude-code adapter wheel so a fresh
  machine can observe and launch out of the box; other adapters install
  later via the (future) node-side extensions story.

## Alternatives Considered

- **NATS / Redis Streams** — rejected above; revisit at real scale.
- **One shared database** (CCANs write to the hub store directly) —
  rejected: couples every machine to hub storage details and violates the
  store-behind-a-Protocol seam (ADR-0003).
- **Hub as the only registry (CCANs forward raw observations)** —
  rejected: makes remote machines useless during hub outages and puts the
  session state machine on the wrong side of a network partition; the
  mirrored-log design keeps each machine autonomous and the hub a view.
