"""Audio I/O seams and the utterance endpointer.

``AudioSource``/``AudioSink`` isolate the pipeline from hardware so tests
drive it with scripted frames. The real microphone/speaker implementations
(``SoundDeviceSource``/``SoundDeviceSink``) import ``sounddevice`` lazily -
it is the optional ``prodeo-mjolnir[audio]`` extra.
"""

import array
import asyncio
import math
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip


@runtime_checkable
class AudioSource(Protocol):
    """A stream of fixed-size mono 16-bit PCM frames (one microphone)."""

    @property
    def sample_rate(self) -> int: ...

    def stream(self) -> AsyncIterator[bytes]: ...


@runtime_checkable
class Drainable(Protocol):
    """Optional capability: discard mic frames buffered during playback.

    A source that queues frames off a hardware callback keeps filling that
    queue *while Mjölnir speaks*; the pipeline calls :meth:`drain` after every
    spoken response so it doesn't consume its own TTS as the next command.
    Sources that can't buffer (test fakes) simply don't implement it and the
    pipeline's ``isinstance`` guard skips them.
    """

    def drain(self) -> None: ...


@runtime_checkable
class AudioSink(Protocol):
    """Plays one clip to completion (the pipeline is half-duplex on purpose:
    not listening while speaking avoids hearing ourselves)."""

    async def play(self, clip: AudioClip) -> None: ...


def rms(frame: bytes) -> float:
    """Root-mean-square amplitude of one 16-bit PCM frame."""
    if not frame:
        return 0.0
    samples = array.array("h", frame)
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class Endpointer:
    """Collects one utterance: speech begins, then trailing silence ends it.

    A tiny energy-based VAD is deliberate for v1 - it is deterministic,
    dependency-free, and good enough directly after a wake word, when the
    user is known to be speaking. Feed frames with :meth:`add` until it
    returns True, then take :meth:`clip`.
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        threshold: float = 300.0,
        silence_after_ms: int = 800,
        max_utterance_ms: int = 12_000,
        max_leading_silence_ms: int = 5_000,
    ) -> None:
        self._sample_rate = sample_rate
        self._threshold = threshold
        self._silence_after_ms = silence_after_ms
        self._max_utterance_ms = max_utterance_ms
        self._max_leading_silence_ms = max_leading_silence_ms
        self._frames: list[bytes] = []
        self._heard_speech = False
        self._silence_ms = 0.0
        self._total_ms = 0.0

    def add(self, frame: bytes) -> bool:
        """Feed one frame; True when the utterance is complete."""
        self._frames.append(frame)
        frame_ms = len(frame) / 2 / self._sample_rate * 1000
        self._total_ms += frame_ms
        if rms(frame) >= self._threshold:
            self._heard_speech = True
            self._silence_ms = 0.0
        else:
            self._silence_ms += frame_ms
        if self._heard_speech:
            return (
                self._silence_ms >= self._silence_after_ms
                or self._total_ms >= self._max_utterance_ms
            )
        return self._total_ms >= self._max_leading_silence_ms

    @property
    def heard_speech(self) -> bool:
        return self._heard_speech

    def clip(self) -> AudioClip:
        return AudioClip(pcm=b"".join(self._frames), sample_rate=self._sample_rate)


class SoundDeviceSource:
    """Microphone frames via PortAudio (``prodeo-mjolnir[audio]``)."""

    def __init__(self, *, sample_rate: int = SAMPLE_RATE, frame_ms: int = 80) -> None:
        self._sample_rate = sample_rate
        self._frame_samples = sample_rate * frame_ms // 1000
        # Hoisted so :meth:`drain` can empty it between the callback (which keeps
        # filling during playback) and the listen loop.
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def drain(self) -> None:
        """Discard every frame buffered so far (non-blocking). Called right
        after Mjölnir speaks so echo captured during playback is thrown away."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def stream(self) -> AsyncIterator[bytes]:
        import sounddevice  # heavy/optional: imported only when a real mic is used

        loop = asyncio.get_running_loop()
        queue = self._queue

        # indata is a CFFI buffer from PortAudio; typed Any because sounddevice
        # ships no stubs and the buffer protocol is all we rely on.
        def on_audio(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            data = bytes(indata)
            with_room = not queue.full()  # drop when the loop falls behind
            if with_room:
                loop.call_soon_threadsafe(queue.put_nowait, data)

        stream = sounddevice.RawInputStream(
            samplerate=self._sample_rate,
            blocksize=self._frame_samples,
            channels=1,
            dtype="int16",
            callback=on_audio,
        )
        with stream:
            while True:
                yield await queue.get()


class SoundDeviceSink:
    """Speaker playback via PortAudio (``prodeo-mjolnir[audio]``)."""

    async def play(self, clip: AudioClip) -> None:
        import sounddevice  # heavy/optional: imported only when a real speaker is used

        def blocking_play() -> None:
            import numpy

            samples = numpy.frombuffer(clip.pcm, dtype=numpy.int16)
            sounddevice.play(samples, samplerate=clip.sample_rate, blocking=True)

        await asyncio.to_thread(blocking_play)
