"""Microphone calibration for the energy-VAD threshold.

The endpointer decides "this frame is speech" by comparing each frame's RMS
loudness to ``MJOLNIR_VAD_THRESHOLD``. That number is entirely mic- and
room-specific: too high and speech never registers (the pipeline waits out the
leading-silence ceiling, then says "I didn't catch that"); too low and room
noise never lets an utterance end (it records to the max-command ceiling every
time). This measures the actual ambient floor and speech level and recommends
a threshold between them, so the user sets it once instead of guessing.

The measurement/recommendation logic is pure and injectable (clock + output)
so it is unit-tested without a microphone.
"""

import math
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from prodeo_mjolnir.audio import AudioSource, rms

ClockFn = Callable[[], float]
OutFn = Callable[[str], None]

#: Speech must be at least this many times the ambient floor to trust the
#: recommendation; below it, the user probably didn't speak up.
_CLEAR_SEPARATION = 2.5
#: Absolute floor for a usable speech reading (guards a near-silent capture).
_MIN_SPEECH_LEVEL = 200.0
#: Keep the recommendation a little above the noise floor even when speech and
#: ambient are close, so ordinary room noise never trips the VAD.
_NOISE_MARGIN = 50.0


@dataclass(frozen=True)
class CalibrationResult:
    ambient_level: float
    speech_level: float
    recommended_threshold: int
    #: False when speech was not clearly louder than the room - the
    #: recommendation is a fallback and the user should retry.
    clear_separation: bool


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile (``fraction`` in [0, 1]); 0.0 if empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * fraction
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def recommend_threshold(ambient: Sequence[float], speech: Sequence[float]) -> CalibrationResult:
    """Recommend a VAD threshold sitting between noise floor and speech level.

    Ambient uses a high percentile (a robust noise ceiling); speech uses an
    upper-middle percentile so the inevitable pauses between words don't drag
    it down. The recommendation is their geometric mean - the natural midpoint
    on the logarithmic scale loudness lives on.
    """
    ambient_level = _percentile(ambient, 0.95)
    speech_level = _percentile(speech, 0.75)
    clear = speech_level >= ambient_level * _CLEAR_SEPARATION and speech_level >= _MIN_SPEECH_LEVEL
    if speech_level > ambient_level:
        recommended = math.sqrt(ambient_level * speech_level)
    else:
        recommended = ambient_level * 2  # no separation: stay above the floor
    recommended = max(recommended, ambient_level + _NOISE_MARGIN)
    return CalibrationResult(
        ambient_level=ambient_level,
        speech_level=speech_level,
        recommended_threshold=round(recommended),
        clear_separation=clear,
    )


async def _collect(frames: AsyncIterator[bytes], *, seconds: float, clock: ClockFn) -> list[float]:
    """Per-frame RMS for ``seconds``, consuming the shared frame iterator."""
    readings: list[float] = []
    start = clock()
    async for frame in frames:
        readings.append(rms(frame))
        if clock() - start >= seconds:
            break
    return readings


async def run_calibration(
    source: AudioSource,
    *,
    ambient_s: float = 3.0,
    speech_s: float = 4.0,
    out: OutFn = print,
    clock: ClockFn = time.monotonic,
) -> CalibrationResult:
    """Guide the user through the two phases and print the recommendation."""
    frames = source.stream()
    try:
        out("Microphone calibration.")
        out(f"  Stay quiet for {ambient_s:.0f} seconds while I measure the room...")
        ambient = await _collect(frames, seconds=ambient_s, clock=clock)
        out('  Now say a typical command, e.g. "Mjolnir, status", until I stop...')
        speech = await _collect(frames, seconds=speech_s, clock=clock)
    finally:
        if isinstance(frames, AsyncGenerator):
            await frames.aclose()  # release the mic promptly

    result = recommend_threshold(ambient, speech)
    out("")
    out(f"  Ambient noise level : ~{result.ambient_level:.0f}")
    out(f"  Speech level        : ~{result.speech_level:.0f}")
    if result.clear_separation:
        out(f"  Recommended threshold: {result.recommended_threshold}")
        out("")
        out("Set it before starting Mjolnir:")
        out(f"  export MJOLNIR_VAD_THRESHOLD={result.recommended_threshold}   # Linux/macOS")
        out(f'  $env:MJOLNIR_VAD_THRESHOLD = "{result.recommended_threshold}"  # Windows')
    else:
        out("  Speech was not clearly louder than the room.")
        out("  Move the microphone closer or speak up, then run --calibrate again.")
    return result
