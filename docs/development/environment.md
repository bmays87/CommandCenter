# Development Environment

## Prerequisites

- Linux (primary target). macOS and Windows (WSL2 recommended) are supported for
  development; nothing in the codebase may call platform-specific APIs directly —
  platform variance is isolated behind interfaces if it ever becomes necessary.
- Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20+ (dashboard only),
  Docker (optional).

## Setup

```bash
git clone https://github.com/prodeo/command-center
cd command-center
uv sync --all-packages          # creates .venv, installs workspace + dev deps
uv run pytest                   # run the test suite
uv run prodeo-server --dev      # start the server with hot reload
cd dashboard && npm ci && npm run dev   # dashboard against the dev server
```

`scripts/bootstrap.sh` performs the above plus git hooks (`pre-commit` running
ruff + mypy on changed files).

## VS Code

`.vscode/` ships recommended settings: Ruff extension as default
formatter/linter, mypy via the dedicated extension, pytest test discovery, and a
compound launch config that starts server + dashboard together.

## Everyday Commands

| Task | Command |
|---|---|
| Lint + format | `uv run ruff check --fix . && uv run ruff format .` |
| Type check | `uv run mypy src packages` |
| Unit tests | `uv run pytest -m "not integration"` |
| Full suite | `uv run pytest` |
| Regenerate API types for dashboard | `uv run scripts/gen_api_types.py` |
| Run in Docker | `docker compose -f docker/compose.yaml up` |
