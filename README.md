# Prodeo Command Center

**An operating system for AI agents.**

Command Center is a headless, local-first platform for supervising, orchestrating,
monitoring, and communicating with multiple AI coding agents simultaneously. It is
not another AI assistant, and it does not replace Claude Code, Codex CLI, Gemini CLI,
Aider, or OpenHands — it manages them.

## Status

**Phase 6 — Many Machines: core landed; deployment recipes remain.** The
hub/CCAN split (ADR-0020) runs: the dashboard shows **one tab per machine**
(renameable, first-connected first), and a machine joins by downloading a
**CCAN installer** from the dashboard — a zip minted per download with this
hub's certificate baked in, so the installed node answers **only** its
parent Command Center (mutual TLS, single-use enrollment tokens,
ADR-0025). Each paired machine runs its agents locally and keeps its own
event log; the hub mirrors that log over pinned HTTPS and routes commands
back — launch on any tab, terminate, prompt, switch models, answer
permissions from one inbox (no broker; ADR-0026). Sessions on two machines
are visible and controllable from one dashboard.

**Phase 5 — Onboarding & Extensibility: complete.** Command Center is
installed and extended from the dashboard, not a shell ritual. The
**extensions manager** browses a sanctioned catalog and installs, enables,
configures, and launches plugins and apps through schema-generated forms;
guided setup installs the **Mjölnir** voice client
([prodeo-mjolnir](packages/prodeo-mjolnir/)) and its engines, downloads the
models, and starts it — no hand-assembled `MJOLNIR_*` env vars. The server
can **launch and drive** Claude Code sessions itself: start a session on a
project, message it, switch model and permission mode live, and answer its
questions — all from the web UI. Mjölnir is free and open-source.

Everything from earlier phases stands: Claude Code, Aider, and Codex CLI
sessions supervised side by side; permission requests answered from the
dashboard, phone, or **out loud** (Mjölnir wakes on its name, answers "what
happened overnight?", and approves permissions by voice, fully offline —
OpenWakeWord + faster-whisper + Piper as engine plugins; Raspberry Pi satellite
runbook in [docs/deployment/satellite-pi.md](docs/deployment/satellite-pi.md));
the scheduler launches agent runs unattended on cron; a daily digest summarizes
the fleet; retention archives old events.

What remains in Phase 6 is **deployment recipes** (the hub as a container,
the CCAN as a systemd unit / Windows service). See the
[roadmap](docs/roadmap.md) for what each phase delivers. Start with
[docs/vision.md](docs/vision.md) and
[docs/architecture/overview.md](docs/architecture/overview.md).

## Quickstart

```bash
uv sync --all-groups
(cd dashboard && npm install && npm run build)   # optional: the web UI
PRODEO_API_TOKEN=change-me uv run prodeo-server
```

Windows (PowerShell):

```powershell
uv sync --all-groups
cd dashboard; npm install; npm run build; cd ..   # optional: the web UI
$env:PRODEO_API_TOKEN = "change-me"
uv run prodeo-server
```

Open `http://127.0.0.1:8600`, enter your token, and any Claude Code session on the
machine (live or historical) appears in the fleet view. The REST API lives under
`/api` (`/api/health`, `/api/sessions`, `/api/events`) with a WebSocket event
stream at `/api/ws/events`; interactive docs at `/docs`.

## Documentation Map

| Document | Purpose |
|---|---|
| [Vision](docs/vision.md) | Why this project exists |
| [Goals & Non-Goals](docs/goals-and-non-goals.md) | Scope boundaries |
| [Architecture Overview](docs/architecture/overview.md) | System design |
| [Event Model](docs/architecture/event-model.md) | Event taxonomy, schema, versioning |
| [Adapter Specification](docs/architecture/adapter-specification.md) | How agents plug in |
| [Plugin System](docs/architecture/plugin-system.md) | Extensibility mechanism |
| [Voice Pipeline](docs/architecture/voice-pipeline.md) | Mjölnir, the voice client |
| [Dashboard Architecture](docs/architecture/dashboard.md) | Web UI design |
| [Repository Layout](docs/architecture/repository-layout.md) | Where code lives |
| [Running the System](docs/deployment/running-the-system.md) | What starts what: processes vs. in-process plugins |
| [Coding Standards](docs/development/coding-standards.md) | How we write code |
| [Development Environment](docs/development/environment.md) | Getting set up |
| [Contributing](docs/contributing.md) | How to contribute |
| [Roadmap](docs/roadmap.md) | Phased milestones |
| [ADRs](docs/adr/) | Architecture Decision Records |

## Core Principles

1. **Headless core.** Voice, dashboard, mobile, and automation are all just clients.
2. **Linux first.** Cross-platform via pure-Python paths, never platform-specific APIs.
3. **Local first.** Fully functional offline; cloud integrations are optional plugins.
4. **Adapter architecture.** The core knows nothing about any specific agent.
5. **Event driven.** Services communicate through events, never direct coupling.
6. **Everything replaceable.** STT, TTS, storage, notifications — all behind interfaces.

## License

[Apache-2.0](LICENSE) — core and all first-party plugins (ADR-0006). The
"Prodeo Command Center" name is retained as a trademark.
