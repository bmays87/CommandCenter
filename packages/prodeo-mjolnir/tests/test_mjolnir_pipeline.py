"""VoicePipeline: wake -> capture -> STT -> intent -> spoken response,
voice.* reporting, attention, and notification speaking."""

import asyncio

import pytest
from mjolnir_fakes import (
    SILENCE_FRAME,
    SPEECH_FRAME,
    WAKE_FRAME,
    FakeServerClient,
    FakeSink,
    FakeStt,
    FakeTts,
    FakeWakeWord,
    ScriptedSource,
    make_interaction,
    make_session,
    settle,
)

from prodeo.events import new_event
from prodeo.events import types as ev
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.config import MjolnirSettings
from prodeo_mjolnir.handlers import CommandHandlers
from prodeo_mjolnir.packs import NEUTRAL
from prodeo_mjolnir.pipeline import VoicePipeline


def _settings(**overrides: object) -> MjolnirSettings:
    defaults: dict[str, object] = {
        "ack_enabled": False,
        "vad_silence_ms": 160,  # two silence frames end the utterance
        "heartbeat_interval_s": 0.02,
        "speak_notifications": "attentive",
        "attentive_window_s": 60.0,
    }
    defaults.update(overrides)
    return MjolnirSettings(**defaults)  # type: ignore[arg-type]  # test-only kwargs passthrough


def _pipeline(
    client: FakeServerClient,
    source: ScriptedSource,
    transcripts: list[str],
    **overrides: object,
) -> tuple[VoicePipeline, FakeTts, FakeSink, FakeStt]:
    settings = _settings(**overrides)
    cache = LocalCache(client.as_client())
    composer = ResponseComposer(NEUTRAL, honorific="sir")
    handlers = CommandHandlers(cache, client.as_client(), composer)
    tts = FakeTts()
    sink = FakeSink()
    stt = FakeStt(transcripts)
    pipeline = VoicePipeline(
        settings,
        wakeword=FakeWakeWord(),
        stt=stt,
        tts=tts,
        source=source,
        sink=sink,
        client=client.as_client(),
        cache=cache,
        handlers=handlers,
        composer=composer,
    )
    return pipeline, tts, sink, stt


EXCHANGE = [WAKE_FRAME, SPEECH_FRAME, SPEECH_FRAME, SILENCE_FRAME, SILENCE_FRAME]


@pytest.mark.asyncio
async def test_full_exchange_speaks_and_reports() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", title="nightly-refactor")]
    pipeline, tts, sink, stt = _pipeline(client, ScriptedSource(EXCHANGE), ["status report"])

    await pipeline.start()
    await settle()
    await pipeline.stop()

    # spoken response came from the real router/handlers/composer chain
    assert tts.texts == ["1 session active, sir: nightly-refactor."]
    assert len(sink.played) == 1
    assert len(stt.clips) == 1  # wake frame itself is not part of the utterance

    # the exchange told its whole story to the server, under one correlation
    types = [e.type for e in client.voice_events]
    assert types == [
        ev.VOICE_WAKE_WORD_DETECTED,
        ev.VOICE_COMMAND_RECEIVED,
        ev.VOICE_TRANSCRIPTION_COMPLETED,
        ev.VOICE_SPEECH_STARTED,
        ev.VOICE_SPEECH_FINISHED,
    ]
    correlations = {e.correlation_id for e in client.voice_events}
    assert len(correlations) == 1 and None not in correlations
    assert client.voice_events[2].payload["text"] == "status report"
    assert pipeline.attentive
    assert client.presence_forgotten  # clean goodbye on stop


@pytest.mark.asyncio
async def test_ack_is_spoken_when_enabled() -> None:
    client = FakeServerClient()
    pipeline, tts, _, _ = _pipeline(
        client, ScriptedSource(EXCHANGE), ["never mind"], ack_enabled=True
    )
    await pipeline.start()
    await settle()
    await pipeline.stop()
    assert tts.texts == ["Yes, sir?", "Very well, sir."]


@pytest.mark.asyncio
async def test_silence_after_wake_apologizes() -> None:
    client = FakeServerClient()
    frames = [WAKE_FRAME] + [SILENCE_FRAME] * 70  # leading-silence timeout (5 s)
    pipeline, tts, _, stt = _pipeline(client, ScriptedSource(frames), ["should not be called"])
    await pipeline.start()
    await settle()
    await pipeline.stop()
    assert tts.texts == ["I didn't catch that, sir."]
    assert stt.clips == []  # nothing was transcribed


@pytest.mark.asyncio
async def test_stt_is_prewarmed_at_startup_without_consuming_a_transcript() -> None:
    client = FakeServerClient()
    pipeline, _, _, stt = _pipeline(client, ScriptedSource([]), ["status report"])

    await pipeline.start()
    await settle()
    await pipeline.stop()

    assert stt.warmups == 1  # model loaded off the critical path
    assert stt.transcripts == ["status report"]  # warm-up did not eat the script


@pytest.mark.asyncio
async def test_heartbeat_reports_presence() -> None:
    client = FakeServerClient()
    pipeline, _, _, _ = _pipeline(client, ScriptedSource([SILENCE_FRAME] * 3), [])
    await pipeline.start()
    await asyncio.sleep(0.08)
    await pipeline.stop()
    assert client.presence_reports  # heartbeats flowed
    assert client.presence_reports[0] is False  # nobody spoke: not attentive


@pytest.mark.asyncio
async def test_notification_spoken_only_when_attentive() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    interaction = make_interaction("i1", "s1", title="May I run the migration?")
    request_event = new_event(
        ev.INTERACTION_REQUESTED,
        session_id="s1",
        payload={"interaction": interaction.model_dump(mode="json")},
    )

    # not attentive (no exchange happened): stays silent
    pipeline, tts, _, _ = _pipeline(client, ScriptedSource([SILENCE_FRAME]), [])
    await pipeline.start()
    client.push(request_event)
    await settle()
    await pipeline.stop()
    assert tts.texts == []

    # after a voice exchange the user is attentive: the request is spoken
    client2 = FakeServerClient()
    client2.sessions = [make_session("s1", project="/repos/db")]
    pipeline2, tts2, _, _ = _pipeline(client2, ScriptedSource(EXCHANGE), ["status"])
    await pipeline2.start()
    await settle()
    client2.push(request_event)
    await settle()
    await pipeline2.stop()
    assert tts2.texts[-1] == "claude-code on db asks, sir: May I run the migration?"


@pytest.mark.asyncio
async def test_notification_modes_always_and_never() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", title="nightly")]
    completed = new_event(ev.SESSION_COMPLETED, session_id="s1", payload={"title": "nightly"})

    pipeline, tts, _, _ = _pipeline(
        client, ScriptedSource([SILENCE_FRAME]), [], speak_notifications="always"
    )
    await pipeline.start()
    client.push(completed)
    await settle()
    await pipeline.stop()
    assert tts.texts == ["nightly has completed, sir."]

    client2 = FakeServerClient()
    client2.sessions = [make_session("s1", title="nightly")]
    pipeline2, tts2, _, _ = _pipeline(
        client2, ScriptedSource(EXCHANGE), ["status"], speak_notifications="never"
    )
    await pipeline2.start()
    await settle()
    client2.push(completed)
    await settle()
    await pipeline2.stop()
    assert all("completed" not in t for t in tts2.texts)
