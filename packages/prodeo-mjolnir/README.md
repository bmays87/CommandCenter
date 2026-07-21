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
client exits at startup with a `voice_path` validation error:

```bash
python -m piper.download_voices en_GB-alan-medium --data-dir ~/piper-voices
```

## Run

Linux/macOS:

```bash
export MJOLNIR_SERVER_URL=http://127.0.0.1:8600
export MJOLNIR_API_TOKEN=...        # the server's PRODEO_API_TOKEN
export MJOLNIR_ENGINES='{"piper": {"voice_path": "~/piper-voices/en_GB-alan-medium.onnx"}}'
prodeo-mjolnir
```

Windows (PowerShell):

```powershell
$env:MJOLNIR_SERVER_URL = "http://127.0.0.1:8600"
$env:MJOLNIR_API_TOKEN = "..."      # the server's PRODEO_API_TOKEN
$env:MJOLNIR_ENGINES = '{"piper": {"voice_path": "C:/path/to/piper-voices/en_GB-alan-medium.onnx"}}'
prodeo-mjolnir
```

Use a full drive-letter path (`C:/...`) in `voice_path` — POSIX-style
`/c/...` paths fail on Windows. Forward slashes are fine and avoid JSON
escaping.

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
(silence that ends an utterance, default 800), `MAX_COMMAND_S`,
`WAKE_THRESHOLD`.

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

## What you can say

"Status", "what happened overnight" (or "good morning"), "anything need
me?", "approve it" / "approve the permission for <project>", "deny it",
"stop <project>", "help", "never mind".
