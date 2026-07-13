# Coding Standards

## Language and Tooling

- Python **3.12+**. Modern syntax throughout (PEP 695 generics where they clarify).
- **uv** for everything: environments, locking, workspace, `uv run` for tasks.
- **Ruff** is both linter and formatter; config in root `pyproject.toml`; no other
  formatters. Lint rule set: `E,F,W,I,N,UP,B,SIM,TCH,RUF` plus `D` (pydoc) on
  public modules.
- **mypy --strict** on `src/`. `Any` requires a `# noqa`-style justification comment.
  Pydantic models give runtime validation at the boundaries; mypy covers the interior.
- **pytest** with `pytest-asyncio` (strict mode) and `anyio`-friendly patterns.
  Coverage gate starts at 80% for core packages.

## Design Rules

1. **Composition over inheritance.** Interfaces are `Protocol`s or small ABCs;
   behavior is injected, not inherited. Deep class hierarchies are a code smell.
2. **Constructor dependency injection, no DI framework.** All wiring happens in the
   composition root (`server.py`). If a class is hard to construct in a test, its
   dependencies are wrong.
3. **Interfaces at seams, not everywhere.** Abstractions exist where we genuinely
   expect substitution (storage, bus, engines, adapters, channels) — see the plugin
   table. Internal helpers do not get speculative interfaces.
4. **Events are the contract.** Changing an event schema follows the versioning rules
   in event-model.md and requires an ADR note if breaking.
5. **Async discipline.** No blocking calls on the event loop; file/subprocess/model
   work goes through `asyncio.to_thread`, executors, or subprocesses. Every
   long-running task has an owner responsible for cancellation on shutdown.
6. **Small modules.** A module that needs a table of contents should be split.
7. **Errors**: domain exceptions inherit `ProdeoError`; adapters/plugins may raise
   anything — the hosting layer contains it and converts to events.
8. **Logging**: structlog, structured key-value only, no f-string interpolation of
   payloads; `session_id`/`correlation_id` bound into context whenever available.

## Repository Hygiene

- Conventional Commits; PRs must keep `main` releasable (CI: ruff, mypy, pytest,
  dashboard typecheck/build, adapter conformance suite).
- Public behavior changes require a docs update in the same PR.
- New dependencies require justification in the PR description; core runtime
  dependencies are kept deliberately short.
