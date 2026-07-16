# prodeo-adapter-claude-code

Teaches Prodeo Command Center to observe and control
[Claude Code](https://claude.com/claude-code) sessions.

- **Observation** (all sessions): watches the JSONL transcripts Claude Code
  writes under its data directory (`~/.claude/projects` by default).
- **Control** (sessions this server launches): launch, terminate, follow-up
  prompts, and permission mediation via the Python
  [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/). A launched
  session's permission requests park on the SDK's `can_use_tool` callback and
  surface in the Command Center inbox; approving or denying there resumes the
  agent (ADR-0008).

Capabilities: **observe**, **historical_sessions**, and — when the SDK is
importable and `control_enabled` is not false — **launch**, **terminate**,
**respond_to_permissions**, **send_prompts**. Sessions the user started
manually in a terminal remain observe-only: their permission prompts cannot be
answered from outside.

## Configuration

Passed through the core's adapter config (`Settings.adapters["claude-code"]`):

| key | default | meaning |
|---|---|---|
| `projects_dir` | `~/.claude/projects` | where Claude Code keeps transcripts |
| `idle_timeout_s` | `1800` | no transcript writes for this long ⇒ session `completed` |
| `poll_interval_s` | `1.0` | transcript tail poll interval |
| `max_replay_bytes` | `524288` | cap on historical bytes replayed per session on first watch |
| `control_enabled` | `true` | set false to force observe-only even with the SDK installed |
| `permission_timeout_s` | server default | mediation timeout for launched sessions' permission requests (timeout ⇒ deny) |

Launching needs the `claude` CLI installed and authenticated on the server's
host — the SDK drives that binary. `LaunchSpec.options` passes extra
`ClaudeAgentOptions` fields through verbatim (e.g. `{"max_turns": 10}`);
unknown keys fail the launch with a clear error.

## Format fragility

The transcript JSONL format is not a stable public contract (ADR-0004). Parsing
is pinned behind a versioned internal parser; unknown record types become opaque
`agent.output_appended` events instead of failures, and a fixture corpus in
`tests/fixtures/` catches upstream drift in CI.

SDK-launched sessions write the same transcripts, so observation flows through
the same watcher; the SDK message stream is used only for control (session id,
permission callbacks, failure detection). See ADR-0008.
