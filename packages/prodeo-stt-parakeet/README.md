# prodeo-stt-parakeet

Mjölnir speech-to-text engine (plugin kind `stt`) backed by NVIDIA Parakeet.
Higher accuracy than the default `prodeo-stt-fasterwhisper`, and — since 0.2.0 —
no heavier to install.

```bash
uv pip install prodeo-stt-parakeet
MJOLNIR_STT_PLUGIN=parakeet
MJOLNIR_ENGINES='{"parakeet": {"model": "nemo-parakeet-tdt-0.6b-v2"}}'
```

Config keys: `model`, `path`, `quantization`, `download_root`, `providers`.

## ONNX Runtime, not NeMo

0.2.0 replaced NVIDIA's NeMo distribution with
[onnx-asr](https://github.com/istupakov/onnx-asr), which runs the same Parakeet
weights through ONNX Runtime. Same model, one dependency:

| | before (NeMo) | now (ONNX) |
|---|---|---|
| packages installed | ~148 incl. PyTorch, transformers, lightning, wandb | **1** (`onnx-asr`) |
| runs on CPU | no | **yes** |
| in the workspace dev group | no — too heavy to test | **yes** |

`onnxruntime` was already present for faster-whisper, Piper, and OpenWakeWord,
so on a normal install this package adds almost nothing. It was also the last
thing pulling PyTorch into the project, which is why `uv sync --all-packages` is
no longer a trap.

## Models

`model` takes an onnx-asr id, not a Hugging Face repo path:

- `nemo-parakeet-tdt-0.6b-v2` (default) — English
- `nemo-parakeet-tdt-0.6b-v3` — multilingual
- `nemo-parakeet-ctc-0.6b`, `nemo-parakeet-rnnt-0.6b` — other decoders

The weights (~600MB) download on first use. Set `download_root` to choose where
they land — it sets `HF_HOME`, and an `HF_HOME` you already exported wins. Set
`path` instead to point at a directory you populated yourself and skip the
download entirely. `quantization: int8` gets a smaller, faster model for some
accuracy.

## GPU — the route that needs no CUDA

The stock `onnxruntime` is CPU-only. On Windows, **DirectML is the recommended
way to use a GPU for speech-to-text**: it needs no CUDA toolkit, no cuDNN, and
works on any DX12 GPU — NVIDIA, AMD, or Intel.

```bash
uv pip install onnxruntime-directml
```

```json
{"parakeet": {"providers": ["DmlExecutionProvider"]}}
```

with `MJOLNIR_STT_PLUGIN=parakeet`.

`onnxruntime`, `onnxruntime-gpu`, and `onnxruntime-directml` **all provide the
same `onnxruntime` module and are mutually exclusive** — installing one
replaces the others, for every engine in the process, not just this one. And
because it is not in the lock, the next `uv sync` puts the stock package back.

For CUDA instead, `uv pip install onnxruntime-gpu` and name
`CUDAExecutionProvider`.

### What this does not cover

- **faster-whisper has no DirectML path.** Its CTranslate2 backend is CUDA-only,
  so if you want *that* engine on the GPU you still need CUDA 12 + cuDNN 9.
  Running Parakeet on DirectML instead avoids the whole toolkit.
- **Piper (TTS) stays on CPU.** Its upstream API exposes only `use_cuda`, with
  no provider selection, so DirectML is not reachable. A non-issue for a 63MB
  voice model.

None of this is required. For 2–5 second voice commands CPU transcription is
already fast; treat the GPU as an upgrade, not a prerequisite.
