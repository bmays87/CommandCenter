"""Microphone calibration: percentile stats, threshold recommendation, and
the guided two-phase run (driven by a fake mic + a stepping clock)."""

import pytest
from mjolnir_fakes import ScriptedSource, frame

from prodeo_mjolnir.calibrate import recommend_threshold, run_calibration


class StepClock:
    """Advances a fixed step per call so the two timed windows split cleanly."""

    def __init__(self, step: float) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        now = self._t
        self._t += self._step
        return now


def test_recommend_sits_between_ambient_and_speech() -> None:
    ambient = [30.0] * 40
    speech = [2000.0] * 40

    result = recommend_threshold(ambient, speech)

    assert result.clear_separation
    assert result.ambient_level < result.recommended_threshold < result.speech_level


def test_recommend_flags_poor_separation() -> None:
    # Speech barely above the room: not trustworthy, stay above the floor.
    ambient = [180.0] * 20
    speech = [220.0] * 20

    result = recommend_threshold(ambient, speech)

    assert not result.clear_separation
    assert result.recommended_threshold > result.ambient_level


def test_recommend_flags_silent_capture() -> None:
    # User never spoke: everything near zero -> unusable, flagged.
    result = recommend_threshold([5.0] * 10, [8.0] * 10)
    assert not result.clear_separation


def test_percentile_ignores_inter_word_pauses() -> None:
    # A speech phase is mostly gaps with bursts of speech; the p75 should
    # reflect the speech, not be dragged to zero by the silences.
    speech = [0.0] * 30 + [1500.0] * 30
    result = recommend_threshold([20.0] * 20, speech)
    assert result.speech_level > 0
    assert result.clear_separation


@pytest.mark.asyncio
async def test_run_calibration_reports_env_var() -> None:
    # 40 quiet frames then 40 loud frames; the stepping clock closes each
    # 3.2s window after exactly 40 frames.
    source = ScriptedSource([frame(30)] * 40 + [frame(2000)] * 40)
    lines: list[str] = []

    result = await run_calibration(
        source,
        ambient_s=3.2,
        speech_s=3.2,
        out=lines.append,
        clock=StepClock(0.08),
    )

    assert result.clear_separation
    assert result.ambient_level < result.recommended_threshold < result.speech_level
    printed = "\n".join(lines)
    assert f"MJOLNIR_VAD_THRESHOLD={result.recommended_threshold}" in printed


@pytest.mark.asyncio
async def test_run_calibration_warns_on_poor_separation() -> None:
    source = ScriptedSource([frame(200)] * 40 + [frame(230)] * 40)
    lines: list[str] = []

    result = await run_calibration(
        source, ambient_s=3.2, speech_s=3.2, out=lines.append, clock=StepClock(0.08)
    )

    assert not result.clear_separation
    printed = "\n".join(lines)
    assert "MJOLNIR_VAD_THRESHOLD" not in printed
    assert "closer" in printed or "speak up" in printed
