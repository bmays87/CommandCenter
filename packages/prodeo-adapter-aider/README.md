# prodeo-adapter-aider

Observes [Aider](https://aider.chat) sessions for Prodeo Command Center by
watching each project's `.aider.chat.history.md` — the markdown log Aider
appends wherever it runs. Observe + historical only: Aider has no control
surface to drive remotely, and the capability flags say so.

## Configure

Aider writes into project directories (there is no central session store), so
tell the adapter which projects to watch:

```bash
export PRODEO_ADAPTERS='{"aider": {"projects": ["/home/me/src/app"]}}'
```

| key | default | |
|---|---|---|
| `projects` | `[]` | project directories to watch |
| `history_filename` | `.aider.chat.history.md` | override if you moved it (`--chat-history-file`) |
| `poll_interval_s` | `1.0` | tail poll cadence |
| `idle_timeout_s` | `1800` | quiet seconds before a session counts as completed |
| `max_replay_bytes` | `262144` | history replayed on first sight of a large log |

## What you get

One session per project (repeat runs in the same project surface as the
session resuming). User prompts (`#### ` lines), assistant responses, applied
edits (as `tool.finished` events), model/version metadata, and turn boundaries
from Aider's token-accounting lines.

The history file is a human-facing log, not a stable format; the parser is
versioned, fixture-tested, and degrades unknown structure to plain output
rather than failing (same posture as ADR-0004).
