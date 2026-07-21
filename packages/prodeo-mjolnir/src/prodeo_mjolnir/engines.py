"""Engine contracts: the seams where voice hardware/model choices plug in.

Engines are plugins (entry-point group ``prodeo.plugins``, kinds ``wakeword``
/ ``stt`` / ``tts``) so heavyweight model stacks never become dependencies of
the client itself - see docs/architecture/voice-pipeline.md. Audio crossing
these seams is always mono 16-bit signed little-endian PCM.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: The pipeline's native rate; sources and engines are configured to match.
SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class AudioClip:
    """A finished utterance or synthesized speech (mono 16-bit PCM)."""

    pcm: bytes
    sample_rate: int = SAMPLE_RATE

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate


@runtime_checkable
class WakeWordDetector(Protocol):
    """Scores successive fixed-size audio frames for the wake word.

    ``process`` is called on the event loop for every mic frame, so it must
    be cheap (the reference OpenWakeWord engine is a small ONNX model).
    """

    @property
    def name(self) -> str: ...

    def process(self, frame: bytes) -> float:
        """Score one frame; >= the configured threshold means 'triggered'."""
        ...

    def reset(self) -> None:
        """Clear internal buffers after a trigger (avoid double-fires)."""
        ...


@runtime_checkable
class SpeechToText(Protocol):
    """Turns one captured utterance into text.

    Implementations own their threading: model inference must not block the
    event loop (``asyncio.to_thread`` in the reference engines).
    """

    @property
    def name(self) -> str: ...

    async def transcribe(self, clip: AudioClip) -> str: ...


@runtime_checkable
class Warmable(Protocol):
    """Optional capability: pre-load an engine's model off the critical path.

    Model-backed engines load lazily on first use, so the first command after
    boot otherwise pays the whole load. The pipeline calls :meth:`warmup` in
    the background at startup when an engine offers it; engines that need no
    warming simply don't implement it.
    """

    async def warmup(self) -> None: ...


@runtime_checkable
class TextToSpeech(Protocol):
    """Turns response text into speech audio."""

    @property
    def name(self) -> str: ...

    async def synthesize(self, text: str) -> AudioClip: ...
