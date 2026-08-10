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

## GPU

The stock `onnxruntime` is CPU-only, and that is the default here. For GPU,
install the matching runtime and name the provider:

```bash
uv pip install onnxruntime-gpu        # CUDA
# or: uv pip install onnxruntime-directml   # any GPU on Windows
```

```json
{"parakeet": {"providers": ["CUDAExecutionProvider"]}}
```

DirectML is worth knowing about on Windows: it needs no CUDA toolkit at all,
which is a materially lower bar than the CUDA 12 + cuDNN 9 that faster-whisper
requires for GPU work.
