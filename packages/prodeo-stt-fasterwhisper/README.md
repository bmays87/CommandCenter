# prodeo-stt-fasterwhisper

Mjölnir speech-to-text engine (plugin kind `stt`) backed by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper). The default
engine: CPU-capable, fully offline once the model is downloaded (first use,
cached under `download_root`).

```bash
MJOLNIR_ENGINES='{"faster-whisper": {"model": "small.en", "compute_type": "int8"}}'
```

Config keys: `model` (default `base.en`), `device` (`cpu`), `compute_type`
(`int8`), `language` (`en`), `beam_size`, `download_root`.

For GPU boxes wanting higher accuracy, see `prodeo-stt-parakeet` — same
plugin kind, brutal NeMo dependency chain, which is exactly why STT engines
are separate packages.
