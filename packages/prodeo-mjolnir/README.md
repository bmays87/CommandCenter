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

## Run

```bash
export MJOLNIR_SERVER_URL=http://127.0.0.1:8600
export MJOLNIR_API_TOKEN=...        # the server's PRODEO_API_TOKEN
prodeo-mjolnir
```

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
