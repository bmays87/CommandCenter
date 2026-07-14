# prodeo-adapter-claude-code

Teaches Prodeo Command Center to observe [Claude Code](https://claude.com/claude-code)
sessions by watching the JSONL transcripts Claude Code writes under its data
directory (`~/.claude/projects` by default).

Capabilities in this release: **observe** and **historical_sessions**. Launch,
terminate, and interaction responses arrive in Phase 2 via hooks and headless mode.

## Configuration

Passed through the core's adapter config (`Settings.adapters["claude-code"]`):

| key | default | meaning |
|---|---|---|
| `projects_dir` | `~/.claude/projects` | where Claude Code keeps transcripts |
| `idle_timeout_s` | `1800` | no transcript writes for this long ⇒ session `completed` |
| `poll_interval_s` | `1.0` | transcript tail poll interval |
| `max_replay_bytes` | `524288` | cap on historical bytes replayed per session on first watch |

## Format fragility

The transcript JSONL format is not a stable public contract (ADR-0004). Parsing
is pinned behind a versioned internal parser; unknown record types become opaque
`agent.output_appended` events instead of failures, and a fixture corpus in
`tests/fixtures/` catches upstream drift in CI.
