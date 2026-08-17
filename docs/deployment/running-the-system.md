# Running the system: what starts what

A common question: "I have a dozen packages — do I start all of them?" No. This
page is the mental model for how the pieces run.

## Two processes, not a dozen

Everything in `packages/` is one of two things: a **process** or an
**in-process plugin** that loads automatically once installed. A
single-machine setup has only **two long-running processes** (plus one
optional external daemon); each *additional* agent machine adds one
`prodeo-ccan`:

| You start | What it is | What runs *inside* it |
|---|---|---|
| `prodeo-server` | The headless core ("the hub"); REST + WebSocket API on `:8600` | The **agent adapters** (`claude-code`, `aider`, `codex`), mediation, event store, the daily-digest **summarizer**, and the **node sync** that mirrors paired machines |
| `prodeo-mjolnir` | The voice client — a network *client* of the server, exactly like the dashboard | The **voice engines**: one **wakeword**, one **STT**, one **TTS**, plus the **LLM personality** summarizer |
| `prodeo-ccan` | *(Phase 6, one per **additional** machine — never on the hub's own)* The Command Center Agent Node: mutual-TLS listener on `:8422` answering only its parent hub. Installed from an installer downloaded from the dashboard, not by hand (ADR-0025). | That machine's **agent adapters**, session registry, mediation, and event log — mirrored into the hub (ADR-0026) |
| Ollama | Mjölnir's **default LLM brain** — an external daemon on `:11434`, **not** part of this repo. Degrades gracefully if absent (grammar still works). | — |

Mjölnir knows nothing about adapters. It observes and controls sessions purely
by calling the server's API ([voice-pipeline.md](../architecture/voice-pipeline.md)).
So the agent-watching packages live in the **server**, and the voice packages
live in **Mjölnir** — two separate processes, possibly on separate machines
(e.g. a Raspberry Pi satellite; see [satellite-pi.md](satellite-pi.md)).

### You no longer have to start Mjölnir yourself

Since Phase 5 the server can supervise it for you (ADR-0015). Mjölnir declares
an `AppManifest` in the `prodeo.apps` entry-point group, and the dashboard's
Extensions page can start, stop, and restart it, with crash-restart backoff
while it is meant to be running. Autostart-with-the-server is a toggle,
**off by default** — a process that listens to a microphone should not start
itself uninvited.

Running it by hand still works and is still the right thing on a satellite,
where the server is on another machine entirely (systemd handles it there).

**Audio is the catch.** A supervised child inherits the server's session, so
voice only works when the server runs in the desktop session you want it to
listen in. As a Windows service it will have no microphone. That is a real
limit of server-launched voice, not something to work around.

## Plugins are in-process and auto-discovered

The engine and adapter packages are **not** processes and are **not** started
by hand. Each exposes a Python entry point in the `prodeo.plugins` group;
whichever host process needs that kind discovers it via `importlib.metadata`
and runs it **inside its own process**. Installing the package is all that is
required — see [plugin-system.md](../architecture/plugin-system.md).

- The **server** loads `adapter`, `notifier`, and `summarizer` kinds. It
  deliberately *skips* voice kinds if they happen to be installed alongside it.
- **Mjölnir** hosts the voice kinds (`wakeword` / `stt` / `tts`) and can also
  use a `summarizer` for persona rephrasing.

Installing no longer has to be a shell step either: the Extensions page installs
from a curated catalog into `<PRODEO_DATA_DIR>/extensions/lib`, which sits
outside `.venv` so `uv sync` cannot delete it. Newly installed plugins *and*
apps need a **server restart** to be discovered — both read entry points once,
at boot — but the restart is a button on that page rather than a trip to the
terminal (ADR-0016), and once an app is known, starting and stopping it is live.

The restart button and the folder picker beside the models directory both
require `PRODEO_API_TOKEN` to be set; without it they answer 403, the same as
every other state-changing endpoint. On Windows, a restart leaves the launching
shell with a fresh prompt while the new server keeps logging to that same
console — expected, and explained in ADR-0016.

## "More packages" ≠ "better Mjölnir"

The extra voice packages are **swappable implementations of a fixed set of
roles**, and Mjölnir uses **exactly one of each**, chosen by config — they do
not stack:

| Role | Options installed | Selected by | Default |
|---|---|---|---|
| Wake word | `openwakeword` | `MJOLNIR_WAKEWORD_PLUGIN` | `openwakeword` |
| Speech-to-text | `faster-whisper` **or** `parakeet` | `MJOLNIR_STT_PLUGIN` | `faster-whisper` |
| Text-to-speech | `piper` | `MJOLNIR_TTS_PLUGIN` | `piper` |
| Persona rephraser | `ollama` *(on by default; ADR-0013)* | `MJOLNIR_PERSONA_REPHRASER` | `ollama` |

`parakeet` is the higher-accuracy alternative to `faster-whisper`; both run on
CPU and both go faster on a GPU. You run one *or* the other, never both.
Per-engine settings go in `MJOLNIR_ENGINES` (JSON).

## Bringing it up locally

```bash
# 0. One-time: install the workspace (server + all in-repo packages)
#    uv sync is exact — it deletes anything in .venv that the lock doesn't
#    call for, hand-installed packages included.
uv sync --all-groups

# 1. Start Mjölnir's brain (its default LLM personality). GPU is used
#    automatically if present.
ollama serve && ollama pull llama3.1:8b

# 2. Start the core (hosts the adapters, serves the API on :8600)
PRODEO_API_TOKEN=change-me uv run prodeo-server

# 3. In another shell, start the voice client (loads its engines in-process,
#    connects to the server). See packages/prodeo-mjolnir/README.md for the
#    one-time Piper voice download and the MJOLNIR_ENGINES value.
export MJOLNIR_SERVER_URL=http://127.0.0.1:8600
export MJOLNIR_API_TOKEN=change-me            # the server's PRODEO_API_TOKEN
export MJOLNIR_ENGINES='{"piper": {"voice_path": "'"$HOME"'/piper-voices/en_GB-alan-medium.onnx"}}'
prodeo-mjolnir
```

Ollama is the default brain but not mandatory: without it, Mjölnir logs
`mjolnir.llm_unreachable` and falls back to the deterministic grammar (basic
commands still work, just no LLM understanding or personality). Point
`MJOLNIR_LLM_BASE_URL` / `MJOLNIR_LLM_MODEL` elsewhere to run the model on
another host or swap it (ADR-0013).

GPU speech-to-text is optional in the same way. `faster-whisper` needs the CUDA
runtime installed **machine-wide** — CUDA Toolkit 12.x plus cuDNN 9, never pip's
`nvidia-*` wheels inside `.venv`, which the next `uv sync` removes. Without it
STT runs on the CPU and logs `stt.cuda_runtime_incomplete`. See
[packages/prodeo-stt-fasterwhisper/README.md](../../packages/prodeo-stt-fasterwhisper/README.md#gpu-cuda)
for the exact libraries and versions; `start-mjolnir.ps1` checks for them on
startup and prints what's missing.

## Answering agent prompts from the dashboard

Interactive Claude Code sessions mirror their permission prompts and questions
into Command Center through the `PermissionRequest` hook (ADR-0011), and since
ADR-0019 an `AskUserQuestion` arrives as a real question — full text, option
buttons — that you answer with one click in the Inbox.

The hook is **presence-gated** by default: if you have touched this machine's
keyboard or mouse in the last 90 seconds, the prompt goes to the terminal and
Command Center never sees it. That is right for the away-from-desk case the
hook was built for, and exactly wrong if your way of working is the dashboard
on the same machine. To opt a machine into **Command-Center-first** mediation:

```powershell
setx PRODEO_PRESENT_THRESHOLD_S 0   # then restart VS Code / the terminal
```

With `0`, every prompt goes to Command Center, and the terminal prompt appears
only if the interaction times out (~10 minutes) or is cancelled. Note the
trade: the keystroke-abort escape (start typing → the terminal prompt takes
over) is disabled too, because machine-wide input cannot distinguish typing in
the browser from typing in the IDE.

That's the whole topology. The dashboard is just another client of the same
server; nothing else is a process you start.
