# prodeo-wakeword-openwakeword

Mjölnir wake word engine (plugin kind `wakeword`) backed by
[OpenWakeWord](https://github.com/dscripka/openWakeWord). Local and
CPU-friendly (small ONNX models).

The default wake word is **"mjölnir"** with its proper Norse pronunciation
("MYOL-neer"), which requires a custom-trained model shipped in this
package's `models/` directory. Until that model lands, the engine falls back
to the stock pretrained `fallback_model` (default `hey_jarvis`) and logs the
substitution. Any other OpenWakeWord model works via config:

```bash
MJOLNIR_WAKE_WORD=hey_jarvis                       # stock model by name
MJOLNIR_ENGINES='{"openwakeword": {"model_path": "/opt/models/custom.onnx"}}'
```

Config keys: `wake_word`, `model_path`, `fallback_model`, `inference_framework`.
