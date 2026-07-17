# prodeo-tts-piper

Mjölnir text-to-speech engine (plugin kind `tts`) backed by
[Piper](https://github.com/OHF-Voice/piper1-gpl). Local, fast on CPU, with a
large stock voice catalogue (the calm-British-AI register lives at
`en_GB-alan-medium` and friends).

```bash
python -m piper.download_voices en_GB-alan-medium --data-dir /opt/piper-voices
MJOLNIR_ENGINES='{"piper": {"voice_path": "/opt/piper-voices/en_GB-alan-medium.onnx"}}'
```

Config keys: `voice_path` (required), `speaker_id`.

Note: `piper-tts` is GPL-3.0-licensed; it is isolated in this plugin package
by design. More expressive engines (XTTS-class) arrive as separate `tts`
plugin packages with their own heavy dependencies. Persona voices must be
original, stock, or licensed — never a clone of a real person's voice
without consent.
