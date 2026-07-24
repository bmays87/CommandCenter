# prodeo-mjolnir

Mjölnir is the Prodeo Command Center **voice client**: wake word → VAD → STT
→ deterministic intent router → REST commands, with responses composed from
persona template packs and spoken via TTS. It is a client, not a subsystem —
it talks to the server over the same REST + WebSocket API as the dashboard
and can run on separate hardware (e.g. a Raspberry Pi satellite; see
`docs/deployment/satellite-pi.md`).

## Install

```bash
uv pip install prodeo-mjolnir[audio]        # [audio] = real mic/speaker (PortAudio)
uv pip install prodeo-wakeword-openwakeword prodeo-stt-fasterwhisper prodeo-tts-piper
```

Engines are plugins (entry-point group `prodeo.plugins`, kinds `wakeword` /
`stt` / `tts`) so heavy model stacks stay out of the client itself. The
reference set above is CPU-only and fully offline.

Piper needs a voice model downloaded once before first run — without it the
client exits at startup with a `voice_path` validation error. Download it to a
known folder in your home directory (used verbatim in the Run step below):

```bash
# Linux/macOS
python -m piper.download_voices en_GB-alan-medium --data-dir ~/piper-voices
```

```powershell
# Windows (PowerShell)
python -m piper.download_voices en_GB-alan-medium --data-dir "$env:USERPROFILE\piper-voices"
```

## Run

Linux/macOS — `~` expands, so this is copy-paste ready:

```bash
export MJOLNIR_SERVER_URL=http://127.0.0.1:8600
export MJOLNIR_API_TOKEN=...        # the server's PRODEO_API_TOKEN
export MJOLNIR_ENGINES='{"piper": {"voice_path": "'"$HOME"'/piper-voices/en_GB-alan-medium.onnx"}}'
prodeo-mjolnir
```

Windows (PowerShell) — build the JSON from `$env:USERPROFILE` so you never
hand-type a path (the single-quoted JSON literal will NOT expand a variable
inside it, so construct the string first):

```powershell
$env:MJOLNIR_SERVER_URL = "http://127.0.0.1:8600"
$env:MJOLNIR_API_TOKEN  = "change-me"     # the server's PRODEO_API_TOKEN
$voice = "$env:USERPROFILE/piper-voices/en_GB-alan-medium.onnx"
$env:MJOLNIR_ENGINES = "{""piper"": {""voice_path"": ""$voice""}}"
prodeo-mjolnir
```

> The `voice_path` must be a real, full path to the `.onnx` file you
> downloaded — Mjölnir loads `<voice_path>` and `<voice_path>.json` beside it.
> A literal `C:/path/to/...` or `~/piper-voices/...` (unexpanded) fails with
> `FileNotFoundError: ...en_GB-alan-medium.onnx.json`. On Windows use a
> drive-letter path with forward slashes (`C:/Users/you/...`); POSIX-style
> `/c/...` paths do not work.

The STT model is pre-loaded in the background at startup, so the first command
after boot is no slower than the rest (before, command #1 paid the whole model
load).

## Tuning responsiveness

If Mjölnir keeps saying **"I didn't catch that"** (without repeating what you
said) or is **slow to respond**, the energy-VAD threshold is mistuned for your
mic and room — not the intent grammar. The endpointer treats a frame as speech
only when its loudness clears `MJOLNIR_VAD_THRESHOLD` (default `300`):

- **Threshold too high** (quiet or distant mic): speech never registers, so it
  waits out the leading-silence ceiling (~5 s) and gives up — hence the delay
  and the "didn't catch that" with nothing transcribed.
- **Threshold too low** (noisy room): an utterance never ends, so it records to
  the max-command ceiling (`MJOLNIR_MAX_COMMAND_S`, default 12 s) every time and
  transcribes mostly noise.

Run the calibrator to get the right number for your setup — it measures the
room, then measures your voice, and prints the value to set:

```bash
prodeo-mjolnir --calibrate
```

Related knobs (all `MJOLNIR_`-prefixed): `VAD_THRESHOLD`, `VAD_SILENCE_MS`
(silence that ends an utterance, default 1000), `MAX_COMMAND_S`,
`WAKE_THRESHOLD`.

If Mjölnir **re-triggers on its own speech** (its reply wakes it again), the mic
is hearing the speaker. The pipeline drains buffered mic frames and mutes wake
scoring for `MJOLNIR_ECHO_COOLDOWN_S` (default 0.4 s) after every reply; raise it
for a louder speaker or a more echoey room.

Configuration is environment variables with the `MJOLNIR_` prefix — see
`prodeo_mjolnir/config.py` for the full list. Highlights:

- `MJOLNIR_WAKE_WORD` — wake word model (default: `mjölnir`, pronounced the
  Norse way, "MYOL-neer"; falls back to a stock model until the custom model
  ships).
- `MJOLNIR_HONORIFIC`, `MJOLNIR_PERSONA_PACK` (`neutral` | `steward`),
  `MJOLNIR_PERSONA_PACK_FILE` — persona; see voice-pipeline.md.
- `MJOLNIR_PERSONA_REPHRASER` — optional `summarizer`-kind plugin (e.g.
  `ollama`) that rephrases the overnight briefing in persona. Never used for
  confirmations.
- `MJOLNIR_SPEAK_NOTIFICATIONS` — `attentive` (default) | `always` | `never`.
- `MJOLNIR_ENGINES` — per-engine JSON config, e.g.
  `'{"piper": {"voice_path": "/opt/voices/en_GB-alan-medium.onnx"}}'`.
- `MJOLNIR_INTENT_ROUTER` — `patterns` (default, deterministic + offline) or
  `llm` to add a constrained Ollama classifier consulted only when the grammar
  doesn't recognize a phrasing (ADR-0012). `llm` mode requires a reachable
  Ollama (`ollama serve` + `ollama pull <model>`).
- `MJOLNIR_LLM_ROUTER_BASE_URL` (default `http://localhost:11434`),
  `MJOLNIR_LLM_ROUTER_MODEL` (default `llama3.2`),
  `MJOLNIR_LLM_ROUTER_TIMEOUT_S` (default `4`) — the LLM router's Ollama
  endpoint, model, and per-call timeout.
- `MJOLNIR_LLM_INTENTS` — the closed set of intents the LLM may emit (JSON
  list). Defaults to the read-only set
  `["status","pending","overnight","help","cancel"]`; add `approve`, `deny`, or
  `stop` to let it classify actions too. Anything outside this set is dropped.
- `MJOLNIR_ECHO_COOLDOWN_S` — post-speech wake-word mute window (default 0.4 s).

## What you can say

"Status" (or "do I have any running sessions"), "what happened overnight" (or
"good morning"), "anything need me?" / "any dialogs waiting for answers",
"approve it" / "approve the permission for <project>", "deny it", "stop
<project>", "help", "never mind".

**Numbered answering.** When several things are waiting, Mjölnir enumerates them
("Two things need you. One: … Two: …"). Answer by position: "approve number
two", "deny the first one", or reply in free text with "respond to one with
looks good" / "tell two go ahead". Positions resolve against exactly what was
just read out.
