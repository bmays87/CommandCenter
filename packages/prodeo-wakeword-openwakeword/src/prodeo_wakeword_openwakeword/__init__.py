"""Mjölnir wake word engine: OpenWakeWord.

Implements the ``WakeWordDetector`` Protocol (``prodeo_mjolnir.engines``).
The default wake word is "mjölnir" spoken the Norse way ("MYOL-neer"), which
needs a custom-trained model shipped in this package's ``models/`` directory;
until that lands, a stock pretrained model (``fallback_model``) serves as the
development fallback - with a loud log line, not a silent substitution.

The ``openwakeword``/``numpy`` imports are lazy (constructor/first frame) so
the manifest stays importable everywhere.
"""

import re
import unicodedata
from importlib import resources
from typing import Any

import structlog
from pydantic import BaseModel

from prodeo.plugins import PluginManifest

_log = structlog.get_logger(__name__)

VERSION = "0.1.0"


class OpenWakeWordConfig(BaseModel):
    """Validated by the engine loader before construction."""

    #: Injected from ``MJOLNIR_WAKE_WORD`` unless set per-engine. A bundled
    #: custom model matching this word is used when present.
    wake_word: str = "mjölnir"
    #: Explicit model path (``.onnx``/``.tflite``); overrides everything.
    model_path: str = ""
    #: Stock pretrained model used while no custom model exists.
    fallback_model: str = "hey_jarvis"
    inference_framework: str = "onnx"


def _slug(word: str) -> str:
    """ "mjölnir" -> "mjolnir": diacritics stripped, non-alnum collapsed."""
    ascii_word = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_word.lower()).strip("_")


def _bundled_model(wake_word: str) -> str:
    """Path to a custom model shipped with this package, or ''."""
    slug = _slug(wake_word)
    models = resources.files("prodeo_wakeword_openwakeword") / "models"
    for suffix in (".onnx", ".tflite"):
        candidate = models / f"{slug}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return ""


class OpenWakeWordDetector:
    """Scores 80 ms PCM frames with an OpenWakeWord model."""

    def __init__(self, config: OpenWakeWordConfig) -> None:
        from openwakeword.model import Model  # heavy: onnxruntime et al.

        self._config = config
        selected = config.model_path or _bundled_model(config.wake_word)
        if not selected:
            _log.warning(
                "openwakeword.fallback_model",
                wake_word=config.wake_word,
                fallback=config.fallback_model,
                hint="no custom model for this wake word; using a stock pretrained model",
            )
            selected = config.fallback_model
        kwargs: dict[str, Any] = {"inference_framework": config.inference_framework}
        try:
            self._model = Model(wakeword_models=[selected], **kwargs)
        except TypeError:  # openwakeword < 0.5 argument name
            self._model = Model(wakeword_model_paths=[selected])

    @property
    def name(self) -> str:
        return "openwakeword"

    def process(self, frame: bytes) -> float:
        import numpy

        scores: dict[str, float] = self._model.predict(numpy.frombuffer(frame, dtype=numpy.int16))
        return float(max(scores.values(), default=0.0))

    def reset(self) -> None:
        reset = getattr(self._model, "reset", None)
        if callable(reset):
            reset()


def create_detector(config: OpenWakeWordConfig) -> OpenWakeWordDetector:
    """Plugin factory: called by the engine loader with validated config."""
    return OpenWakeWordDetector(config)


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group): what this plugin is."""
    return PluginManifest(
        name="openwakeword",
        kind="wakeword",
        version=VERSION,
        config_model=OpenWakeWordConfig,
        factory=create_detector,
    )


__all__ = ["OpenWakeWordConfig", "OpenWakeWordDetector", "create_detector", "manifest"]
