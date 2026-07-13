# Contributing

Thank you for considering a contribution. Command Center is documentation-first:
significant changes start as an issue or ADR draft, not as a surprise PR.

## Ways to Contribute

- **Adapters** are the most valuable contribution. Start from
  `examples/adapter-skeleton/`, implement the `AgentAdapter` protocol, and pass the
  conformance suite (`prodeo.adapters.testing`). Adapters live in their own
  repositories or in `packages/` for first-party ones.
- **Plugins** (notifiers, engines, storage backends) follow the same pattern with
  their respective interfaces.
- **Core changes** that alter interfaces, events, or architecture require an ADR
  (copy `docs/adr/template.md`, open a PR for discussion before implementing).

## Pull Request Checklist

1. `uv run ruff check . && uv run mypy src packages && uv run pytest` all green.
2. Tests accompany behavior changes; event schema changes follow
   `docs/architecture/event-model.md` versioning rules.
3. Docs updated in the same PR when public behavior changes.
4. Conventional Commit messages; one logical change per PR.

## Code of Conduct

Contributor Covenant v2.1 applies to all project spaces.

## Governance (initial)

BDFL-style maintainer group while the project is young; decisions are recorded in
ADRs so the reasoning outlives the people. This section will be revisited once there
are regular external contributors.
