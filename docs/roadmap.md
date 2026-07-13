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

## Phase 3 — Orchestrate & Extend
Scheduler (cron-style agent launches); plugin packaging guide + `adapter-skeleton`
example; second and third adapters (Aider, Codex CLI or OpenHands — chosen by
observability of their session formats); daily-summary plugin (Ollama); retention
policies and event archiving.
**Exit:** two different vendors' agents supervised side by side; a scheduled agent
run happens unattended and is summarized.

## Phase 4 — Voice
`prodeo-voice` client: OpenWakeWord + STT plugins (faster-whisper default,
Parakeet optional) + Piper TTS; deterministic intent router; attention-aware
notification routing; satellite deployment docs (Pi).
**Exit:** the vision.md morning scenario works end to end, offline.

## Phase 5 — Many Machines
`EventBus` implementation over NATS (or Redis Streams — ADR at the time); node
identity + remote agent nodes reporting to a hub; dashboard multi-node fleet view;
deployment recipes (Docker, systemd, Home Assistant add-on).
**Exit:** sessions on two machines visible and controllable from one dashboard.

## Later / Icebox
Plugin index with signing; Kubernetes operator; Git/Docker integrations as plugins;
multi-user auth; mobile apps (the API is the product — apps may come from the
community first).
