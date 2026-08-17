# Phase 6 plan addition: per-machine tabs, CCAN onboarding, installer distribution

- **Date**: 2026-08-15
- **Extends**: roadmap "Many Machines" phase; ADR-0020 (the CCAN split)
- **Status**: requirements — decisions recorded here get ratified in the
  Phase 6 ADRs (transport/auth) when implementation starts

Phase 6 as written covers the split itself (hub vs. CCAN, transport, auth,
deployment recipes). This document adds the product surface: how a user sees
many machines in the dashboard, and how a machine becomes a CCAN in the first
place — entirely from the UI, with no shell ritual (the Phase 5 principle
applied to machines).

## 1. Machine tabs

- Directly below the header navigation bar (`nav.topbar`,
  `dashboard/src/App.tsx`), the dashboard gains a **tab strip: one tab per
  connected CCAN**. The strip lives on the machines (fleet) view; Events,
  Extensions, and Inbox remain global.
- The **default (first) tab is the first CCAN that was connected**; tabs
  follow the order machines were added.
- A tab is **titled with the CCAN's machine name by default** and is
  **renameable by the user**. Renames are stored hub-side on the machine
  record (not browser-local), so every dashboard client sees the same names.
  The underlying node identity (`Event.node` / `PRODEO_NODE_NAME`) is not
  changed by a rename — the tab title is a display name.
- Selecting a tab scopes the view to that machine. Each tab shows, for its
  CCAN only:
  - **active sessions**,
  - **agent session history**,
  - the **add-new-session button** (launch routes to that CCAN, per the
    ADR-0020 due-out that machine actions become node-targeted).
  Switching tabs switches all three together — the tab is the machine
  context for everything on the page.
- Data plumbing: sessions and events already carry `Event.node`; the fleet
  and history queries gain a node filter, and the current `FleetView`
  becomes the body of a tab rather than the whole page.

## 2. Empty state — no CCAN configured

When no CCAN devices are configured, the first (only) tab is a mostly empty
page whose center is the onboarding flow:

- An **"Add Machine"** button, centered. To its **right**, a description
  explaining that adding a machine requires the **FQDN or IP address** of a
  machine already running CCAN.
- Directly **below** "Add Machine": a **"Download CCAN Installer"** button.
  Activating it displays the available CCAN installers — the path a user
  takes first when no machine is running CCAN yet: download the installer
  here, install it on the target machine, come back and Add Machine.

## 3. Installer distribution — spawned from the UI

- **Command Center itself produces the CCAN installers and serves them from
  the UI.** The hub is the only distribution channel: installers are
  generated (or finalized) per hub instance at download time, not fetched
  from a generic public artifact — because each installer is **packaged with
  the public certificate of the parent Command Center application** (and the
  hub's address) so the CCAN it installs can establish HTTPS with, and
  recognize, exactly that hub.
- **Aim for a platform-agnostic installer** (a single artifact that runs on
  Windows/Linux/macOS — e.g. Python-launcher-based, since CCAN shares the
  uv/Python toolchain). If platform-specific installers prove necessary,
  the "Download CCAN Installer" button presents the **list** of installers,
  labeled by platform; with exactly one artifact it downloads directly.

## 4. Trust model — parent-only pairing

- **A CCAN responds only to the Command Center application that spawned its
  installer.** No connection is trusted unless it originates from the parent
  hub. Concretely: the packaged hub certificate is the CCAN's trust anchor —
  the CCAN verifies every peer against it and rejects everything else
  (including other Command Center instances).
- All CCAN↔hub traffic is HTTPS; the packaged certificate makes the channel
  verifiable in both roles (the CCAN pins the hub when dialing out; a caller
  must prove the hub identity when reaching in). This satisfies ADR-0020's
  "its own authentication to the hub" due-out from the CCAN side; the hub
  side (how the hub knows a given CCAN is one of *its* installs — e.g. an
  enrollment token minted at installer download) is decided in the Phase 6
  auth ADR.
- Direction, as decided in ADR-0025: **pairing** is hub → CCAN at the
  FQDN/IP (mutual TLS, client-cert-gated). Whether *routine* traffic stays
  hub → node or flips to a persistent outbound channel from the CCAN
  (ADR-0020's hook pattern) is workstream C's transport ADR; the
  parent-only rule holds either way.

## 5. Implementation sequencing (2026-08-15)

Four workstreams, in order; each leaves the system runnable and green.

- **A — Machine registry + per-machine tabs (colocated).** *Landed
  2026-08-15 (ADR-0024).* A `machine.*` event namespace and a hub-side
  machine registry (event-sourced, rebuilt on boot, like schedules); the
  colocated node auto-registers as the first machine (resolving the
  upgrade-path question: yes). `/api/machines` (list/add/rename/remove),
  `Session.node`, the tab strip, per-machine session scoping, the empty
  state. Add Machine answers an honest 501 and the installer list is empty
  with a note until B exists; both contracts are final.
- **B — CCAN package + pairing + installer generation.** *Landed
  2026-08-16 (ADR-0025).* `packages/prodeo-ccan` (mutual-TLS listener,
  parent cert as the whole trust store); hub identity minted at first boot
  (`prodeo.identity`); installers built per download (stdlib `install.py`
  + first-party wheels + hub cert + single-use, node-bound enrollment
  token); Add Machine performs the two-way handshake and records the
  CCAN's certificate for C to pin. Rotation = re-pair (ADR-0025).
- **C — Node-targeted capabilities + event transport.** *Landed 2026-08-17
  (ADR-0026).* No broker: each CCAN runs the full local stack
  (`NodeHost`) and keeps its own log; the hub's NodeSync mirrors it over
  pinned HTTPS long-polls (per-node derived cursor) and folds
  session/interaction facts into hub read-models — the owning node stays
  the only writer. Session commands, launches (`machine_id`), adapter
  listings, and inbox answers route to the owning CCAN via NodeGateway;
  post-pairing calls pin the certificate recorded at pairing. The
  installer bundles the claude-code adapter. Remaining machine-bound
  inventory (browse, extension install onto nodes, host probes, models
  dir) migrates as needed in D/follow-ups.
- **D — Deployment recipes.** Hub container; CCAN as a systemd unit /
  Windows service; docs.

## 6. Open questions (answer before implementation)

- **Colocated upgrade path** — *resolved in A (ADR-0024)*: the hub's own
  node auto-registers on boot (named from `PRODEO_NODE_NAME`), so an
  upgraded deployment lands on a one-tab fleet, never the empty state.
- **Certificate lifecycle** — *resolved in B (ADR-0025)*: first-boot
  self-signed, ten-year validity; rotation is re-pairing (new installers,
  reinstall on each node). User-supplied certificates deferred.
- **Removing a machine** — *resolved in A (ADR-0024)*: DELETE forgets the
  record only; the node's sessions and events stay in the log. The hub's
  own machine cannot be removed (409).
