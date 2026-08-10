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
uv sync --all-groups            # creates .venv, installs workspace + dev deps
uv run pytest                   # run the test suite
uv run prodeo-server --dev      # start the server with hot reload
cd dashboard && npm ci && npm run dev   # dashboard against the dev server
```

`--all-groups` installs every workspace package plus the dev tooling, and is
what CI runs. (`--all-packages` also works now that no member drags in a heavy
stack — until 2026-08-09 it pulled `prodeo-stt-parakeet`'s multi-GB NeMo/torch
chain, which is why older docs warn against it. Parakeet moved to ONNX Runtime
and joined the dev group.)

`uv sync` is *exact*: it makes `.venv` match `uv.lock` plus the selected groups
and **removes anything else it finds**, including packages you installed by hand
with `pip`/`uv pip`. Anything that must survive belongs in a `pyproject.toml`;
anything genuinely system-level (CUDA — see
[running-the-system.md](../deployment/running-the-system.md)) belongs outside the
virtualenv entirely.

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

Run the server directly on the machine you want supervised — the adapters
discover sessions from host-local paths, so a container cannot see them without
mounting away its own isolation. See `docker/README.md`.
