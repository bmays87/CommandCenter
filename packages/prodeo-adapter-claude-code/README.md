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
manually in a terminal are observed only, but their *permission prompts* can
still reach the inbox through the `prodeo-claude-hook` described below
(ADR-0011).

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

## Interactive-session permissions: `prodeo-claude-hook`

Interactive terminal sessions get inbox/voice mediation through a
presence-gated [`PermissionRequest` hook](https://code.claude.com/docs/en/hooks)
(ADR-0011). Install it into `~/.claude/settings.json` (timestamped backup,
idempotent, unrelated keys preserved):

```bash
prodeo-claude-hook --install            # or --print-config to apply by hand
```

Run it on the machine where the interactive sessions run, with the hook's
environment pointing at the Command Center server:

| variable | default | meaning |
|---|---|---|
| `PRODEO_SERVER_URL` | `http://127.0.0.1:8600` | Command Center base URL |
| `PRODEO_API_TOKEN` | *(unset)* | bearer token; set in the *environment*, never in settings.json |
| `PRODEO_HOOK_TIMEOUT_S` | `570` | how long a mediated card waits before falling back to the terminal prompt (keep under Claude Code's 600 s hook cap) |
| `PRODEO_PRESENT_THRESHOLD_S` | `90` | local input newer than this ⇒ "at station" ⇒ instant terminal prompt |
| `PRODEO_MANAGED` | *(set by the launcher only)* | `1` makes the hook pass through — SDK-launched sessions are already mediated |

Presence semantics: on Windows the hook asks `GetLastInputInfo` — note it is
machine-wide, so typing in *any* application counts as "at station". On every
other platform presence is unknowable and the hook always mediates. While a
card is pending, input resuming locally aborts the mediation (the card is
withdrawn) and the terminal prompt takes over; the whole path is fail-open —
server down or timed out simply means the normal terminal prompt.

Minimum Claude Code version: verified against **2.1.212**. On ≥ 2.1.212 the
terminal dialog is drawn concurrently with the hook, so the terminal stays
answerable even mid-mediation (first answer wins); on the older
run-hook-before-prompt contract the presence gate is what keeps at-station
prompts instant. Re-verify the hook contract when upgrading Claude Code.

## Format fragility

The transcript JSONL format is not a stable public contract (ADR-0004). Parsing
is pinned behind a versioned internal parser; unknown record types become opaque
`agent.output_appended` events instead of failures, and a fixture corpus in
`tests/fixtures/` catches upstream drift in CI.

SDK-launched sessions write the same transcripts, so observation flows through
the same watcher; the SDK message stream is used only for control (session id,
permission callbacks, failure detection). See ADR-0008.
