# prodeo-adapter-codex

Observes [OpenAI Codex CLI](https://github.com/openai/codex) sessions for
Prodeo Command Center by watching the rollout files Codex writes under
`~/.codex/sessions/YYYY/MM/DD/` — its own append-only JSONL resume format,
with a `session_meta` header and typed records for messages, tool calls, and
task lifecycle. Observe + historical only for now (Codex's control surfaces
are launch-time choices we cannot attach to after the fact).

## Configure

Works with no config if Codex uses its default home. Otherwise:

```bash
export PRODEO_ADAPTERS='{"codex": {"sessions_dir": "/custom/.codex/sessions"}}'
```

| key | default | |
|---|---|---|
| `sessions_dir` | `~/.codex/sessions` | the date-sharded rollout tree |
| `poll_interval_s` | `1.0` | tail poll cadence |
| `idle_timeout_s` | `1800` | quiet seconds before a session counts as completed |
| `max_replay_bytes` | `524288` | history replayed on first sight of a large rollout |

## What you get

One session per rollout: user/assistant messages, `shell`/`apply_patch`/custom
tool calls with success/failure (from exit codes), turn boundaries from
task lifecycle events, and metadata (cwd as project, model, git branch, CLI
version). Codex's synthetic context messages (`<environment_context>`, …) and
reasoning records are not surfaced; unknown record types degrade to opaque
output rather than failing (ADR-0004 posture).
