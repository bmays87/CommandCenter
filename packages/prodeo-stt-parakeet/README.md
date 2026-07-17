# prodeo-stt-parakeet

Mjölnir speech-to-text engine (plugin kind `stt`) backed by NVIDIA Parakeet
through NeMo. Higher accuracy than the default `prodeo-stt-fasterwhisper`,
at the price of a multi-GB, CUDA-bound dependency chain — which is exactly
why STT engines are separate plugin packages and this one is **not** in the
workspace dev group. Install it only on the machine with the GPU:

```bash
uv pip install prodeo-stt-parakeet
MJOLNIR_STT_PLUGIN=parakeet
MJOLNIR_ENGINES='{"parakeet": {"model": "nvidia/parakeet-tdt-0.6b-v2"}}'
```

Config keys: `model`.
