# Roadmap

Each phase ends with a tagged release, a runnable system, and green CI. Nothing in a
later phase blocks a user of an earlier phase.

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
(faster-whisper default, Parakeet optional) + Piper TTS; wake word defaults to
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

## Phase 5 — Many Machines
`EventBus` implementation over NATS (or Redis Streams — ADR at the time); node
identity + remote agent nodes reporting to a hub; dashboard multi-node fleet view;
deployment recipes (Docker, systemd, Home Assistant add-on).
**Exit:** sessions on two machines visible and controllable from one dashboard.

## Later / Icebox
Plugin index with signing; Kubernetes operator; Git/Docker integrations as plugins;
multi-user auth; mobile apps (the API is the product — apps may come from the
community first).
