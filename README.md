# Prodeo Command Center

**An operating system for AI agents.**

Command Center is a headless, local-first platform for supervising, orchestrating,
monitoring, and communicating with multiple AI coding agents simultaneously. It is
not another AI assistant, and it does not replace Claude Code, Codex CLI, Gemini CLI,
Aider, or OpenHands — it manages them.

## Status

**Pre-implementation.** The project is currently in its documentation-first design
phase. No production code exists yet. Start with [docs/vision.md](docs/vision.md)
and [docs/architecture/overview.md](docs/architecture/overview.md).

## Documentation Map

| Document | Purpose |
|---|---|
| [Vision](docs/vision.md) | Why this project exists |
| [Goals & Non-Goals](docs/goals-and-non-goals.md) | Scope boundaries |
| [Architecture Overview](docs/architecture/overview.md) | System design |
| [Event Model](docs/architecture/event-model.md) | Event taxonomy, schema, versioning |
| [Adapter Specification](docs/architecture/adapter-specification.md) | How agents plug in |
| [Plugin System](docs/architecture/plugin-system.md) | Extensibility mechanism |
| [Voice Pipeline](docs/architecture/voice-pipeline.md) | Voice as a client (future) |
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
