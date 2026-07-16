# ADR-0009: Second and third adapters are Aider and Codex CLI

- **Status**: Accepted
- **Date**: 2026-07-16

## Context

The Phase 3 roadmap calls for two more adapters, "chosen by observability of
their session formats" from the candidates Aider, Codex CLI, and OpenHands.
Our adapter strategy (ADR-0004) is filesystem observation first: an agent
qualifies by leaving a session trail we can discover and tail without
cooperation from the agent.

## Decision

Ship `prodeo-adapter-aider` and `prodeo-adapter-codex`, both observe +
historical only.

- **Codex CLI** has the best observability of the three: every session is an
  append-only JSONL "rollout" under `~/.codex/sessions/YYYY/MM/DD/` with a
  `session_meta` header (id, cwd, git, versions) and typed records for
  messages, tool calls, and task lifecycle — structurally the same shape as
  Claude Code transcripts, so the proven tail-and-parse design transfers
  directly.
- **Aider** has no session store, but appends a markdown log
  (`.aider.chat.history.md`) to every project it runs in, with recognizable
  user-prompt (`#### `) and info (`> `) markers. Observability is good enough
  given explicit project configuration, and Aider exercises the adapter model
  differently: a human-facing markdown format, project-scoped sessions, and
  buffered (not record-based) parsing — useful proof that the contract is not
  secretly shaped around JSONL streams.
- **OpenHands** was passed over, not rejected: its event store is
  server-managed per-conversation JSON, typically behind Docker volumes, so
  simple filesystem tailing is less universal. It remains a candidate for a
  later adapter, plausibly control-first via its REST API.

## Consequences

Two very different vendors' agents are supervised side by side with zero core
changes — the Phase 3 exit criterion and the real test of the adapter
abstraction. Both adapters are pinned behind versioned parsers with fixture
corpora (ADR-0004 posture), because neither format is a public contract.
Control (launching Aider/Codex runs) is deliberately out of scope: neither
tool exposes a control surface we can attach to after the fact, and dishonest
capability flags are worse than absent features.
