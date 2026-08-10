# prodeo-stt-fasterwhisper

Mjölnir speech-to-text engine (plugin kind `stt`) backed by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper). The default
engine: CPU-capable, fully offline once the model is downloaded (first use,
cached under `download_root`).

```bash
MJOLNIR_ENGINES='{"faster-whisper": {"model": "small.en", "compute_type": "int8"}}'
```

Config keys: `model` (default `base.en`), `device` (`auto`), `compute_type`
(`auto`), `language` (`en`), `beam_size`, `download_root`.

`device: auto` uses CUDA when a GPU is visible, else CPU; `compute_type: auto`
follows with `float16` on GPU and `int8` on CPU. Pin either to override.

## GPU (CUDA)

**Only needed for this engine.** CTranslate2 has no DirectML or ROCm path, so
CUDA is the sole route to running faster-whisper on a GPU. If you want GPU
speech-to-text without a ~3GB toolkit, use `prodeo-stt-parakeet` with DirectML
instead — same protocol, any DX12 GPU, no CUDA. And for short voice commands,
CPU `int8` here is already fast enough that neither may be worth the trouble.

CUDA is a **machine-wide prerequisite, not a Python dependency of this package**.
It is deliberately not declared in `pyproject.toml`: `uv sync` is exact and
deletes anything the lock doesn't call for, so `pip install nvidia-cublas-cu12
nvidia-cudnn-cu12` into `.venv` works right up until the next sync silently
removes it and STT drops back to the CPU.

Two libraries are needed, neither of which CTranslate2 ships:

| Library | Comes from | Why |
|---|---|---|
| `cublas64_12.dll` | CUDA Toolkit **12.x** | Direct import of `ctranslate2.dll` |
| `cudnn_ops64_9.dll` (+ `cudnn_graph`/`cnn`/`adv`) | cuDNN **9** | The wheel bundles only the `cudnn64_9` dispatcher, which dlopens these |

CUDA **13 does not work** — CTranslate2 4.x links `cublas64_12.dll`, so the
`winget` default (13.x) must be overridden:

```powershell
winget install --id Nvidia.CUDA -e --version 12.9
# cuDNN 9 is a separate download: https://developer.nvidia.com/cudnn-downloads
```

`start-mjolnir.ps1` runs a preflight that reports exactly which of these is
missing when an NVIDIA GPU is present (skip it with `-NoCudaCheck`). At load
time the engine puts the runtime on the loader path itself, searching, in order:
versioned `CUDA_PATH_V12_*`/`CUDA_PATH_V13_*` env vars, `%ProgramFiles%\NVIDIA
GPU Computing Toolkit\CUDA\v1[23].*\bin`, the cuDNN 9 tree under
`%ProgramFiles%\NVIDIA\CUDNN`, `CUDNN_PATH`, and finally the `nvidia-*` pip
wheels if present. Bare `CUDA_PATH` is ignored on purpose — it points at
whichever toolkit was installed last, often an older major with no
`cublas64_12.dll`.

If the runtime is incomplete the engine logs `stt.cuda_runtime_incomplete`, and
`device: auto` resolves straight to CPU — a visible driver (`nvcuda.dll`) alone
is not enough, because CTranslate2 loads cuBLAS/cuDNN *lazily at the first
encode*, after model construction appears to succeed. Whenever CUDA *is*
attempted (pinned, or auto with the runtime seemingly present), the engine
verifies the model with a short probe transcription at load time; on failure it
logs `stt.cuda_unavailable_fallback` and rebuilds on CPU. Transcription keeps
working either way — just slower.

For higher accuracy, see `prodeo-stt-parakeet` — same plugin kind, also
CPU-capable since it moved to ONNX Runtime, and notably able to use a GPU
through DirectML without any CUDA toolkit.
