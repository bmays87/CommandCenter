# ADR-0020: The Command Center Agent Node (CCAN) — direction and first seam

- **Status**: Accepted (direction; implementation lands with Phase 6)
- **Date**: 2026-08-10
- **Informs**: roadmap Phase 6 (Many Machines)
- **Note (2026-08-15)**: the editor-opening action — the seam's first tenant
  and the motivating example below — was removed: it existed only as a human
  convenience, and an IDE plays no role in an agent's development.
  `prodeo.machine` is gone with it. The rule stands: machine-bound capability
  goes behind a Protocol wired in the composition root; the seam is
  re-instantiated by Phase 6's first real node-targeted action (session
  launch routing).

## Context

Command Center's core is, by definition, platform-independent — an event log,
a session registry, mediation, an API, a dashboard. Nothing about it needs to
run on the machine where agents work, and the natural deployment for it is a
container. But a growing set of features is unavoidably machine-bound: adapters
watch transcript files, the app supervisor spawns Mjölnir, the extensions
manager installs packages and stores models, host probes read the registry,
and now the dashboard can open a project in VS Code — which only means
anything on the machine whose VS Code it is.

Today one process does both jobs on one machine. The product decision
(2026-08-10): split them. The hub runs anywhere, containerized; a per-machine
**Command Center Agent Node (CCAN)** is installed on every machine that runs
AI agents, owns everything machine-bound, and talks to the hub. Several CCANs,
one hub, one dashboard — multiple projects and agents across multiple
machines, driven from one place.

## Decision

### 1. The split, by responsibility

**Hub** (containerizable): event bus + durable log, session registry,
mediation, scheduler, summaries, notifications, presence, the REST/WS API,
the dashboard, the extension *catalog*.

**CCAN** (one per agent machine): adapters and transcript watching, agent
launch (the SDK), app supervision (Mjölnir and successors), filesystem
browsing, models storage and asset downloads, extension installation into its
own environment, host probes (GPU/CUDA), and editor opening. Every one of
these answers a question about, or performs an action on, *a particular
machine*.

The permission hook (ADR-0011) is the proof of shape: it already runs on the
agent machine and talks inward to the hub API over authenticated HTTP,
long-polling for a human's answer. CCAN generalizes that pattern.

### 2. Seams now, split later

Phase 6 builds the node; this ADR commits the codebase to not fighting it:

- `prodeo.machine.MachineActions` is the first explicit seam: machine-local
  actions behind a Protocol, wired only in the composition root. Opening the
  editor goes through it today; a remote (CCAN-backed) implementation replaces
  the local one *at that single wiring point* when the split lands.
- New machine-bound capability goes behind that seam (or a sibling), never
  inline in API handlers.
- The groundwork that already exists is the other half: `Event.node` on every
  envelope, `EventBus` as a Protocol for a broker-backed implementation
  (ADR-0002), presence carrying node identity, the satellite runbook.

### 3. Due-outs recorded for Phase 6

- Machine actions become node-targeted: "open this project" routes to the
  CCAN that owns the project; session launch routes to the machine the
  project lives on.
- CCAN gets an install story (one command per machine) and its own
  authentication to the hub.
- The machine-bound inventory (browse, apps, extension install, models dir,
  host probes, restart) migrates from the hub process to CCAN.

## Consequences

- Until Phase 6, nothing user-visible changes: hub and CCAN are colocated in
  one process, and `LocalMachineActions` is the whole "node".
- The dashboard and API never learn machine specifics; they call capabilities
  that happen to be local today and remote tomorrow.
- The hub container story (Docker/compose) becomes writable without waiting
  for the split, since everything machine-bound is behind seams or already an
  external process.

## Alternatives Considered

- **Keep one process and require it to run on the agent machine forever.**
  Rejected: it caps the system at one machine and makes the container story
  a lie.
- **Split immediately.** Rejected for this change: a node package, transport,
  and auth story is Phase 6-sized; the features users need now should not
  wait on it, and the seam makes the wait free.
- **SSH/remote-exec from the hub instead of a node.** Rejected: credentials
  and shell access where a narrow, typed capability suffices; the hook
  pattern (outbound HTTPS from the machine) is already proven here.
