# ADR-0024: Machine registry and the per-machine dashboard

- **Status**: Accepted
- **Date**: 2026-08-15
- **Extends**: ADR-0020 (the CCAN split); Phase 6 plan
  `docs/plans/2026-08-15-phase6-machines-ui-and-ccan-onboarding.md`

## Context

Phase 6 makes machines a first-class concept: the dashboard grows one tab per
agent machine, tabs are renameable, and remote machines join by pairing with
a CCAN. That needs a durable, hub-side catalogue of machines — something the
system has never had. Node identity existed only as a string stamped on event
envelopes (`Event.node`) and presence heartbeats; sessions carried no machine
identity at all, and nothing listed "the machines this hub knows".

## Decision

### 1. An event-sourced Machine Registry

`prodeo.machines.MachineRegistry` is a core service and the only writer of a
new `machine.*` namespace: `machine.added` (full `Machine` dump),
`machine.renamed` (`machine_id`, `name`), `machine.removed` (`machine_id`).
It folds the log on boot, exactly like schedules — chosen over a JSON file
because renames must be durable facts every dashboard client sees, and only
the event log has a live push path (the WebSocket stream). `machine.*` joins
the retention-protected namespaces.

A `Machine` is `{id, node, name, address, added_at}`. `node` is the identity
(`PRODEO_NODE_NAME`, what events carry); `name` is the tab's display name and
the only renameable field — renaming never touches `node`, so history stays
attributed. `address` is the FQDN/IP its CCAN answers on; `None` means the
hub's own host.

### 2. The hub's machine registers itself

On boot, after the rebuild, the hub ensures a record for its own node exists
(`ensure_local`, address `None`). An upgraded single-machine deployment
therefore lands on a one-tab fleet, never on the empty "Add Machine" state,
and running sessions stay visible. The hub's own machine cannot be removed
(409): removing it would orphan the colocated adapters. Removing a *remote*
machine only forgets the record — its sessions and events stay in the log.

### 3. Sessions carry their node

`Session` gains `node` (additive, default `"local"` — no version bump per
the event-model rules; pre-existing stored events rebuild as `"local"`,
which is what they were). The registry stamps it at creation; the
descriptive-update fold never touches it. This is what scopes each dashboard
tab: the fleet filters sessions by the selected machine's node.

### 4. The API is shaped for pairing before pairing exists

`GET/POST /api/machines`, `PUT /api/machines/{id}/name`,
`DELETE /api/machines/{id}`, and `GET /api/ccan/installers` land now, all
token-gated for writes. `POST /api/machines` (pair by address) answers **501
with an explanation**, and the installer list is **empty with a note**, until
the CCAN package (workstream B) provides the handshake and the artifacts.
Honest refusals were chosen over stubbed success or hiding the UI: the
request/response shapes are final, so B changes behavior, not contracts.

## Consequences

- The dashboard's fleet is per-machine from now on; a second machine is a
  tab appearing, not a redesign.
- `machine.added` for a remote machine is published only by the future
  pairing path, so every machine in the catalogue was either the hub itself
  or explicitly paired — the registry never invents machines from observed
  event nodes. (Events from unknown nodes remain queryable; they simply have
  no tab yet.)
- Workstream C gives `MachineRegistry` its sibling: the re-instantiated
  machine-actions seam (ADR-0020) keyed by machine, so "launch here" routes
  to the owning CCAN.

## Alternatives Considered

- **Derive machines from observed `Event.node` values.** Rejected: a
  catalogue you can rename and remove from needs explicit membership;
  deriving it would resurrect removed machines from old events and could
  never carry an address or pairing state.
- **A JSON file in the data dir (like extensions.json).** Rejected: renames
  would be invisible to other connected dashboards without inventing a
  second change-notification path; the event log already is one.
- **Hide the Add Machine flow until pairing works.** Rejected: the flow is
  the product spec's centerpiece and the honest 501/empty-list keeps the UI,
  API, and docs aligned while B is built.
