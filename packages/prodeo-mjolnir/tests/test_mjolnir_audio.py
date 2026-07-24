"""Endpointer (energy VAD) and PCM helpers."""

import array
import math

from prodeo_mjolnir.audio import Drainable, Endpointer, SoundDeviceSource, rms
from prodeo_mjolnir.engines import SAMPLE_RATE

FRAME_MS = 80
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def _frame(amplitude: int) -> bytes:
    """One frame of a square-ish wave with the given peak amplitude."""
    samples = array.array(
        "h", [amplitude if i % 2 == 0 else -amplitude for i in range(FRAME_SAMPLES)]
    )
    return samples.tobytes()


SPEECH = _frame(2000)
SILENCE = _frame(0)


def test_rms() -> None:
    assert rms(SILENCE) == 0.0
    assert math.isclose(rms(SPEECH), 2000.0)
    assert rms(b"") == 0.0


def test_utterance_ends_after_trailing_silence() -> None:
    ep = Endpointer(threshold=300, silence_after_ms=160, max_utterance_ms=10_000)
    frames = [SPEECH, SPEECH, SILENCE, SILENCE]
    results = [ep.add(f) for f in frames]
    assert results == [False, False, False, True]
    assert ep.heard_speech
    clip = ep.clip()
    assert clip.pcm == b"".join(frames)
    assert clip.duration_s == len(frames) * FRAME_MS / 1000


def test_speech_resets_the_silence_clock() -> None:
    ep = Endpointer(threshold=300, silence_after_ms=160, max_utterance_ms=10_000)
    assert not ep.add(SPEECH)
    assert not ep.add(SILENCE)  # 80 ms silence: not yet
    assert not ep.add(SPEECH)  # speech again: clock resets
    assert not ep.add(SILENCE)
    assert ep.add(SILENCE)


def test_max_utterance_cuts_off() -> None:
    ep = Endpointer(threshold=300, silence_after_ms=100_000, max_utterance_ms=240)
    assert not ep.add(SPEECH)
    assert not ep.add(SPEECH)
    assert ep.add(SPEECH)


def test_no_speech_times_out_without_hearing() -> None:
    ep = Endpointer(threshold=300, silence_after_ms=160, max_leading_silence_ms=240)
    assert not ep.add(SILENCE)
    assert not ep.add(SILENCE)
    assert ep.add(SILENCE)
    assert not ep.heard_speech


def test_sound_device_source_drain_empties_buffered_frames() -> None:
    source = SoundDeviceSource(sample_rate=SAMPLE_RATE, frame_ms=FRAME_MS)
    assert isinstance(source, Drainable)  # advertises the optional capability
    for _ in range(5):
        source._queue.put_nowait(SPEECH)
    assert source._queue.qsize() == 5

    source.drain()
    assert source._queue.qsize() == 0
    source.drain()  # draining an empty queue is a no-op, never blocks
    assert source._queue.qsize() == 0
