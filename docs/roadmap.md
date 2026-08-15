# Roadmap

Each phase ends with a tagged release, a runnable system, and green CI. Nothing in a
later phase blocks a user of an earlier phase.

Phases are resequenced when priorities change; the number is the order we intend to
build in, not an identity. Docs and ADRs therefore refer to phases by **name**
("the Many Machines phase") so a reordering does not silently falsify them.
Most recent change: **Onboarding & Extensibility and Many Machines swapped on
2026-08-09** — see the note under Many Machines.

## Phase 0 — Foundations (docs + skeleton)
Repository scaffolding, CI (ruff, mypy, pytest, dashboard build), event envelope and
core schemas, `EventBus` interface + in-process implementation, SQLite `EventStore`,
config loading, composition root that boots and logs `system.started`.
**Exit:** `uv run prodeo-server` starts; `pytest` green; docs published.

## Phase 1 — Observe (Milestone 1 from the brief)
Session Registry + state machine; Adapter Manager + `AdapterContext`; adapter
conformance test kit; **claude-code adapter (observe + historical)** via transcript
watching; REST API (`/sessions`, `/events`, `/health`) + WebSocket event stream;
dashboard fleet view + session view (read-only); single-token auth.
**Exit:** a user running Claude Code sessions sees them live in the dashboard.

## Phase 2 — Mediate & Control
Interaction model (`interaction.*` events, exactly-once resolution); claude-code
adapter gains launch/terminate/respond via hooks + headless mode; dashboard
interaction inbox and answer controls; Notifier service + first channels (desktop,
ntfy, webhook); event explorer; optional MongoDB storage backend plugin.
**Exit:** a user approves an agent's permission request from the dashboard or phone.
*Shipped* with deliberate deviations: claude-code control uses the Agent SDK
(ADR-0008) — headless path done; blocking-hook mediation of *interactive*
sessions was deferred here and later shipped in Phase 4 via the external
interaction API + presence-gated `PermissionRequest` hook (ADR-0011);
channels are log + ntfy + desktop (webhook dropped for now); MongoDB
deferred, but the EventStore contract suite (ADR-0003) shipped as its gate.

## Phase 3 — Orchestrate & Extend
Scheduler (cron-style agent launches); plugin packaging guide + `adapter-skeleton`
example; second and third adapters (Aider, Codex CLI or OpenHands — chosen by
observability of their session formats); daily-summary plugin (Ollama); retention
policies and event archiving.
**Exit:** two different vendors' agents supervised side by side; a scheduled agent
run happens unattended and is summarized.
*Shipped* with deliberate deviations: Aider + Codex CLI chosen, both
observe-only (ADR-0009; OpenHands deferred); the scheduler is a core service,
not a plugin kind (no substitution demand yet); the formal Plugin Host landed
with `adapter`/`notifier`/`summarizer` kinds and the packaging guide; the
daily summary is a core service whose *prose* comes from the optional
`prodeo-summarizer-ollama` plugin — the digest works without it.

## Phase 4 — Voice
**Mjölnir** (`prodeo-mjolnir`) voice client: OpenWakeWord + STT plugins
(faster-whisper default, Parakeet for accuracy) + Piper TTS; wake word defaults to
the proper pronunciation of "mjölnir" and is user-configurable; deterministic
intent router; attention-aware notification routing; satellite deployment
docs (Pi).
**Exit:** the vision.md morning scenario works end to end, offline.
*Shipped* with deliberate deviations: engines are plugins in the shared
`prodeo.plugins` group but hosted by the mjolnir process — the server host
skips the voice kinds (ADR-0010); presence/attention is an ephemeral core
service (`/api/presence`, never event-logged) feeding away-only channel
suppression (`notification.suppressed`); persona shipped as template packs
(`neutral`, `steward`) + honorific + optional summarizer-kind rephraser for
briefings only; the custom "mjölnir" wake word model is still to be trained —
a stock OpenWakeWord model is the loudly-logged fallback; the exit scenario
is pinned by `tests/integration/test_voice_flow.py` (fake mic/engines, real
server, real HTTP + WebSocket).

## Phase 5 — Onboarding & Extensibility (web-first setup)
Make installing and extending Command Center a task in the dashboard, not a
shell ritual — motivated by voice setup today needing hand-assembled `MJOLNIR_*`
env vars, manual model downloads, and manual CUDA-runtime installs.
- **Extensions manager** in the dashboard: browse, install, enable/disable, and
  configure plugins (adapters, notifiers, summarizers, voice engines) through
  forms generated from each plugin's `config_model` schema — the same
  `PluginManifest`/entry-point mechanism (Phase 3), surfaced in the UI. A step
  toward the signed plugin index (Icebox). **Any install that writes bulk data
  (models, runtimes, extension payloads) must prompt for a target
  path/drive — never silently assume the system drive.** (The
  `start-mjolnir.ps1 -ModelsPath` flag is the interim CLI form of this.)
  Extensions are modelled in two classes — in-process `plugin` and
  out-of-process `app` (Mjölnir) — so the app path is not forced into a plugin
  kind (ADR-0014, ADR-0015).
- **Guided voice-client setup**: install Mjölnir and its engine plugins,
  download the STT/TTS and wake-word models, write the config, and
  launch/register the client from the web UI — replacing the manual env vars
  and model downloads.
- **Proactive environment provisioning**: detect hardware and optional
  dependencies and set up the best path instead of silently degrading — an
  NVIDIA GPU offers a one-click CUDA-runtime install (cuBLAS/cuDNN) so STT runs
  on the GPU rather than falling back to CPU; a missing engine library is
  surfaced with a fix action, not a stack trace.
**Exit:** a new user installs the dashboard, adds the Claude Code adapter and
the voice client, and approves a permission by voice — without opening a
terminal.
*Shipped* with deliberate deviations: installs are **catalog-gated** — the API
takes a sanctioned name, never a caller-supplied package spec, so it cannot
fetch arbitrary code (ADR-0015), and every state-changing endpoint is refused
when `PRODEO_API_TOKEN` is unset. First-party packages are unpublished and
depend on each other, so the server builds the workspace into a local wheel
index and installs with `--find-links`; extensions land in
`<PRODEO_DATA_DIR>/extensions/lib`, outside `.venv` where `uv sync` cannot
delete them. Mjölnir is an **app-class extension**, and **free** — it is part
of the open-source project (it was briefly on a `paid` tier; that discouraged
contributors and was reverted 2026-08-10). The paid-tier mechanism remains for
a future paid extension: its entitlement check is presence of a licence key and
is explicitly a placeholder, not a security control. The supervisor treats a clean child exit as
non-terminal and gives up after five consecutive crash-loops. Model assets are
catalog data verified by the files they *produce*, which is what catches a
Piper voice downloaded without its `.onnx.json` sibling — the failure that
starts the client mute. `prodeo-stt-parakeet` moved from NeMo to ONNX Runtime
(148 packages to 1) and joined the dev group, which also removed the last
thing pulling PyTorch into the workspace. Environment provisioning reports
GPU/CUDA/audio/Ollama with fix actions rather than one-click installers, and
**DirectML via Parakeet is the documented GPU route** — no CUDA toolkit at
all; CUDA remains necessary only for faster-whisper, which has no other GPU
path.

## Phase 6 — Many Machines (hub + CCAN)
The architecture is the **CCAN split** (ADR-0020): the hub — events, sessions,
mediation, API, dashboard — is platform-independent and runs in a container;
a **Command Center Agent Node (CCAN)** is installed on every machine that runs
AI agents and owns everything machine-bound (adapters/transcript watching,
agent launch, app supervision, filesystem browse, models storage, host
probes). One hub, many CCANs, one dashboard.
Work: the CCAN installable + its hub authentication; `EventBus` over NATS (or
Redis Streams — ADR at the time); dashboard multi-node fleet view; deployment
recipes (hub container, CCAN via systemd / Windows).
**UI & onboarding spec (2026-08-15):**
`docs/plans/2026-08-15-phase6-machines-ui-and-ccan-onboarding.md` — a
per-machine tab strip below the header nav (one tab per CCAN, renameable,
each showing that machine's active sessions, history, and new-session
button); an Add Machine empty state (FQDN/IP of a machine running CCAN);
installers generated and downloaded from the UI, packaged with the hub's
public certificate so a CCAN trusts **only** its parent Command Center —
platform-agnostic if possible, listed per platform otherwise.
**Due-outs from Phase 5.5 (2026-08-10):** machine actions become node-targeted —
"launch a session here" routes to the CCAN owning the project; the
machine-bound endpoint inventory is listed in ADR-0020. (The `MachineActions`
seam's first tenant, editor opening, was removed 2026-08-15 — an IDE plays no
role in an agent's work; see the note in ADR-0020. The seam pattern returns
with the first real node-targeted action.)
**Exit:** sessions on two machines visible and controllable from one dashboard.
*Deferred* — swapped with Onboarding & Extensibility on 2026-08-09: there is no
multi-machine use case on the horizon, while the single-machine setup ritual is a
daily cost. Nothing is lost by waiting — the groundwork is already in place and
was designed for it: node identity is on every event envelope
(`Event.node`, `PRODEO_NODE_NAME`), `EventBus` is a Protocol precisely so a
broker-backed implementation can arrive without touching services (ADR-0002),
and the permission hook already has the CCAN shape (machine-local process
talking inward over authenticated HTTP, ADR-0011). The earlier note that
containerizing fights the adapters' host-local premise (`docker/README.md`)
is *resolved* by the split: the hub containerizes, the CCAN stays on the host.

## Later / Icebox
Plugin index with signing; Kubernetes operator; Git/Docker integrations as plugins;
multi-user auth; mobile apps (the API is the product — apps may come from the
community first).
