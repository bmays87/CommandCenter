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
- Open tension to resolve in that ADR: "Add Machine takes an FQDN/IP"
  implies the hub dials the CCAN, while ADR-0020 favors the proven
  outbound-only hook pattern (ADR-0011). Candidate reconciliation: the
  FQDN/IP is used for initial pairing/verification, after which the CCAN
  holds a persistent outbound channel; either way the parent-only rule above
  is non-negotiable.

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
- **B — CCAN package + pairing + installer generation.** New workspace
  package `packages/prodeo-ccan` (the node daemon); hub certificate
  generated at first boot; installer built and served by the hub with the
  cert + hub address baked in; Add Machine completes the pairing handshake.
  ADR: pairing/trust (parent-only rule, §4).
- **C — Node-targeted capabilities + event transport.** Session launch and
  the machine-bound inventory (ADR-0020) route to the owning CCAN; CCAN
  events flow into the hub log with their node identity. The broker
  question (NATS vs Redis Streams vs plain HTTPS ingestion like
  `/api/voice/events`) gets its ADR here, decided against real traffic.
- **D — Deployment recipes.** Hub container; CCAN as a systemd unit /
  Windows service; docs.

## 6. Open questions (answer before implementation)

- **Colocated upgrade path** — *resolved in A (ADR-0024)*: the hub's own
  node auto-registers on boot (named from `PRODEO_NODE_NAME`), so an
  upgraded deployment lands on a one-tab fleet, never the empty state.
- **Certificate lifecycle**: what generates the hub certificate (first-boot
  self-signed vs. user-supplied), and what happens to already-paired CCANs
  when it rotates. Decide in B's pairing ADR.
- **Removing a machine** — *resolved in A (ADR-0024)*: DELETE forgets the
  record only; the node's sessions and events stay in the log. The hub's
  own machine cannot be removed (409).
