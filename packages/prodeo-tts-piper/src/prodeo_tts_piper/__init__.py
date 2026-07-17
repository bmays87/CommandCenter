"""Mjölnir TTS engine: Piper (local neural text-to-speech).

Implements the ``TextToSpeech`` Protocol (``prodeo_mjolnir.engines``). Piper's
stock voice catalogue covers the calm-British-AI register the persona docs
mention (``en_GB-alan-medium``, ``en_GB-northern_english_male``, ...);
voices are downloaded once with ``python -m piper.download_voices <voice>``
and referenced by path. Synthesis runs in a worker thread; the voice model
is loaded on first use and cached.
"""

import asyncio
import threading
from typing import Any

from pydantic import BaseModel

from prodeo.plugins import PluginManifest
from prodeo_mjolnir.engines import AudioClip

VERSION = "0.1.0"


class PiperTtsConfig(BaseModel):
    """Validated by the engine loader before construction."""

    #: Path to the voice model (``.onnx``; its ``.onnx.json`` config must sit
    #: alongside). Download with ``python -m piper.download_voices <voice>``.
    voice_path: str
    #: For multi-speaker voices; None = the voice's default speaker.
    speaker_id: int | None = None


class PiperTts:
    """One Piper synthesis per response."""

    def __init__(self, config: PiperTtsConfig) -> None:
        self._config = config
        self._voice: Any = None  # PiperVoice; Any keeps the import lazy
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "piper"

    async def synthesize(self, text: str) -> AudioClip:
        return await asyncio.to_thread(self._synthesize_sync, text)

    def _synthesize_sync(self, text: str) -> AudioClip:
        voice = self._ensure_voice()
        kwargs: dict[str, Any] = {}
        if self._config.speaker_id is not None:
            kwargs["speaker_id"] = self._config.speaker_id
        pcm = bytearray()
        sample_rate = int(voice.config.sample_rate)
        for chunk in voice.synthesize(text, **kwargs):
            pcm.extend(chunk.audio_int16_bytes)
            sample_rate = int(getattr(chunk, "sample_rate", sample_rate))
        return AudioClip(pcm=bytes(pcm), sample_rate=sample_rate)

    def _ensure_voice(self) -> Any:  # PiperVoice; Any keeps the import lazy
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice  # heavy: onnxruntime et al.

                self._voice = PiperVoice.load(self._config.voice_path)
            return self._voice


def create_tts(config: PiperTtsConfig) -> PiperTts:
    """Plugin factory: called by the engine loader with validated config."""
    return PiperTts(config)


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group): what this plugin is."""
    return PluginManifest(
        name="piper",
        kind="tts",
        version=VERSION,
        config_model=PiperTtsConfig,
        factory=create_tts,
    )


__all__ = ["PiperTts", "PiperTtsConfig", "create_tts", "manifest"]
