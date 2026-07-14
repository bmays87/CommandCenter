# Prodeo Command Center — agent guide

An operating system for AI agents: a headless core that observes and (later)
controls coding-agent sessions, with all clients (dashboard, voice, mobile)
talking to one event-driven API.

## Read first

- `docs/roadmap.md` — what the current phase is and what's in scope.
- `docs/development/coding-standards.md` — binding rules (mypy --strict, Ruff,
  Protocol-based seams, constructor DI, async discipline, structlog).
- `docs/architecture/event-model.md` — events are the contract; follow the
  naming and versioning rules exactly.
- `docs/architecture/adapter-specification.md` before touching adapters.

## Hard rules

- The core (`src/prodeo`) must contain **zero** agent-specific logic; if core
  code needs `if adapter.name == ...`, the design is wrong.
- `src/prodeo/server.py` is the only place concrete implementations are wired.
- Services never import each other; they publish/subscribe events and expose
  query interfaces injected via constructors.
- Every adapter must pass `prodeo.adapters.testing.AdapterConformanceSuite`.
- Public behavior changes require a docs update in the same change.

## Commands

```bash
uv sync --all-groups          # includes workspace packages (packages/*)
uv run pytest -q              # unit + conformance + integration
uv run mypy                   # strict, covers core and adapter packages
uv run ruff check . && uv run ruff format --check .
uv run prodeo-server          # boots the daemon (PRODEO_* env for config)
```

Dashboard (`dashboard/`): `npm run build` (tsc strict + vite). API types are
generated: `uv run python scripts/export_openapi.py` then `npm run generate`
inside `dashboard/` — regenerate both whenever API models change, CI diffs them.

## Layout

- `src/prodeo/{events,bus,persistence,sessions,adapters,api}` — core; unit
  tests live next to code in `tests/` subpackages.
- `packages/prodeo-adapter-claude-code/` — first adapter (uv workspace member).
- `tests/integration/` — cross-component tests against the composed server.
