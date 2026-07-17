# Prodeo Command Center

**An operating system for AI agents.**

Command Center is a headless, local-first platform for supervising, orchestrating,
monitoring, and communicating with multiple AI coding agents simultaneously. It is
not another AI assistant, and it does not replace Claude Code, Codex CLI, Gemini CLI,
Aider, or OpenHands — it manages them.

## Status

**Phase 4 — Voice.** Claude Code, Aider, and Codex CLI sessions are
supervised side by side; permission requests are answered from the dashboard,
phone, or **out loud** — the Mjölnir voice client
([prodeo-mjolnir](packages/prodeo-mjolnir/)) wakes on its name, answers
"what happened overnight?", and approves permissions by voice, fully offline
(OpenWakeWord + faster-whisper + Piper as engine plugins; Raspberry Pi
satellite runbook in [docs/deployment/satellite-pi.md](docs/deployment/satellite-pi.md)).
The scheduler launches agent runs unattended on cron; a daily digest
summarizes the fleet; retention archives old events. See the
[roadmap](docs/roadmap.md) for what each phase delivers. Start with
[docs/vision.md](docs/vision.md) and
[docs/architecture/overview.md](docs/architecture/overview.md).

## Quickstart

```bash
uv sync --all-groups
(cd dashboard && npm install && npm run build)   # optional: the web UI
PRODEO_API_TOKEN=change-me uv run prodeo-server
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

Apache-2.0 (proposed — see ADR-0006).
