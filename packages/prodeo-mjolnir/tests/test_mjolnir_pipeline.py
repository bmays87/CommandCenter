"""VoicePipeline: wake -> capture -> STT -> intent -> spoken response,
voice.* reporting, attention, and notification speaking."""

import asyncio

import pytest
from mjolnir_fakes import (
    SILENCE_FRAME,
    SPEECH_FRAME,
    WAKE_FRAME,
    DrainableSource,
    FakeServerClient,
    FakeSink,
    FakeStt,
    FakeTts,
    FakeWakeWord,
    PushSource,
    ScriptedSource,
    make_interaction,
    make_session,
    settle,
)

from prodeo.events import new_event
from prodeo.events import types as ev
from prodeo_mjolnir.audio import AudioSource
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
    source: AudioSource,
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


def _wakes(client: FakeServerClient) -> int:
    return sum(1 for e in client.voice_events if e.type == ev.VOICE_WAKE_WORD_DETECTED)


@pytest.mark.asyncio
async def test_echo_cooldown_suppresses_self_trigger() -> None:
    """After Mjölnir speaks, wake frames arriving inside the cooldown window
    (its own TTS bleeding speaker->mic) must not open a second exchange."""
    client = FakeServerClient()
    client.sessions = [make_session("s1", title="nightly-refactor")]
    # One real exchange, then an echo burst that looks like a wake + command.
    frames = [*EXCHANGE, WAKE_FRAME, SPEECH_FRAME, SPEECH_FRAME, SILENCE_FRAME, SILENCE_FRAME]
    pipeline, _, _, stt = _pipeline(
        client,
        ScriptedSource(frames),  # no drain(): only the cooldown can suppress
        ["status", "SHOULD NOT RUN"],
        echo_cooldown_s=5.0,
    )

    await pipeline.start()
    await settle()
    await pipeline.stop()

    assert _wakes(client) == 1  # the echo never counted as a wake
    assert len(stt.clips) == 1  # and never reached transcription
    assert stt.transcripts == ["SHOULD NOT RUN"]  # second transcript untouched


# ------------------------------------------------ follow-up listening (ADR-0023)


def _two_pending(client: FakeServerClient) -> None:
    client.sessions = [
        make_session("s1", project="/repos/db", active_ago_s=30),
        make_session("s2", project="/repos/api", active_ago_s=60),
    ]
    client.interactions = [
        make_interaction("i1", "s1", title="Run the migration?"),
        make_interaction("i2", "s2", title="Delete fixtures?"),
    ]


REPLY_FRAMES = [SPEECH_FRAME, SPEECH_FRAME, SILENCE_FRAME, SILENCE_FRAME]


@pytest.mark.asyncio
async def test_followup_window_captures_reply_without_wake() -> None:
    """A reply that expects a follow-up opens the no-wake window: the next
    utterance is captured without a wake word, no ack, same correlation."""
    client = FakeServerClient()
    _two_pending(client)
    # "approve" with two pending reads them out and keeps listening; the
    # follow-up approves by position - no second WAKE_FRAME anywhere.
    pipeline, tts, _, stt = _pipeline(
        client,
        ScriptedSource([*EXCHANGE, *REPLY_FRAMES]),
        ["approve", "approve number two"],
        echo_cooldown_s=0.0,
        ack_enabled=True,  # the ack must NOT be spoken for the follow-up
    )

    await pipeline.start()
    await settle()
    await pipeline.stop()

    assert _wakes(client) == 1  # one wake for two utterances
    assert len(stt.clips) == 2
    assert client.answered == [("i2", "allow")]
    assert tts.texts[0] == "Yes, sir?"  # ack for the woken exchange only
    assert tts.texts[-1] == "Approved, sir."
    assert sum(1 for t in tts.texts if t == "Yes, sir?") == 1

    received = [e for e in client.voice_events if e.type == ev.VOICE_COMMAND_RECEIVED]
    assert [e.payload["followup"] for e in received] == [False, True]
    # the follow-up stays in the same exchange's correlation chain
    assert received[0].correlation_id == received[1].correlation_id


@pytest.mark.asyncio
async def test_expired_followup_window_restores_wake_only() -> None:
    client = FakeServerClient()
    _two_pending(client)
    pipeline, _, _, stt = _pipeline(
        client,
        ScriptedSource([*EXCHANGE, *REPLY_FRAMES]),
        ["approve", "SHOULD NOT RUN"],
        echo_cooldown_s=0.0,
        followup_window_s=0.0,  # the window is already over when frames arrive
    )

    await pipeline.start()
    await settle()
    await pipeline.stop()

    assert len(stt.clips) == 1  # speech without a wake word was ignored
    assert stt.transcripts == ["SHOULD NOT RUN"]
    assert client.answered == []


@pytest.mark.asyncio
async def test_echo_cooldown_wins_over_the_followup_window() -> None:
    """The mute check stays ahead of the follow-up check: frames arriving
    inside the cooldown are drained even though a window is open, so TTS
    tail can neither self-trigger nor leak into the reply clip."""
    client = FakeServerClient()
    _two_pending(client)
    pipeline, _, _, stt = _pipeline(
        client,
        ScriptedSource([*EXCHANGE, *REPLY_FRAMES]),
        ["approve", "SHOULD NOT RUN"],
        echo_cooldown_s=5.0,  # every post-reply frame lands inside the cooldown
        followup_window_s=60.0,
    )

    await pipeline.start()
    await settle()
    await pipeline.stop()

    assert len(stt.clips) == 1  # nothing captured during the mute
    assert client.answered == []


@pytest.mark.asyncio
async def test_silence_in_the_followup_window_says_nothing() -> None:
    """A declined invitation is not an error: no "I didn't catch that"."""
    client = FakeServerClient()
    _two_pending(client)
    pipeline, tts, _, stt = _pipeline(
        client,
        ScriptedSource([*EXCHANGE] + [SILENCE_FRAME] * 70),  # silence: 5s timeout
        ["approve"],
        echo_cooldown_s=0.0,
    )

    await pipeline.start()
    await settle()
    await pipeline.stop()

    assert len(tts.texts) == 1  # only the pending readout spoke
    assert all("didn't catch" not in t for t in tts.texts)
    assert stt.transcripts == []  # the readout consumed the only transcript


@pytest.mark.asyncio
async def test_interaction_notification_opens_window_and_answers_by_option() -> None:
    """After announcing a question, the very next utterance (no wake word)
    can answer it by option label."""
    from prodeo.mediation import InteractionKind

    client = FakeServerClient()
    client.sessions = [make_session("s1", project="/repos/db")]
    question = make_interaction(
        "i1",
        "s1",
        title="Which approach?",
        kind=InteractionKind.QUESTION,
        options=["Safe path", "Fast path"],
    )
    client.interactions = [question]
    source = PushSource()
    pipeline, tts, _, _ = _pipeline(
        client,
        source,
        ["the fast path"],
        speak_notifications="always",
        echo_cooldown_s=0.0,
    )

    await pipeline.start()
    await settle()  # cache primed (the pending question is known)
    client.push(
        new_event(
            ev.INTERACTION_REQUESTED,
            session_id="s1",
            payload={"interaction": question.model_dump(mode="json")},
        )
    )
    await settle()  # announcement spoken; window + question context open
    source.push(*REPLY_FRAMES)
    await settle()
    source.close()
    await pipeline.stop()

    assert _wakes(client) == 0  # never woken - the announcement invited the reply
    assert client.answered_text == [("i1", "Fast path")]
    assert tts.texts[-1] == "Answered, sir."


@pytest.mark.asyncio
async def test_completed_notification_does_not_open_the_window() -> None:
    client = FakeServerClient()
    client.sessions = [make_session("s1", title="nightly")]
    source = PushSource()
    pipeline, _, _, stt = _pipeline(
        client,
        source,
        ["SHOULD NOT RUN"],
        speak_notifications="always",
        echo_cooldown_s=0.0,
    )

    await pipeline.start()
    client.push(new_event(ev.SESSION_COMPLETED, session_id="s1", payload={"title": "nightly"}))
    await settle()
    source.push(*REPLY_FRAMES)  # speech with no wake word
    await settle()
    source.close()
    await pipeline.stop()

    assert stt.clips == []  # a completion announcement invites no reply
    assert stt.transcripts == ["SHOULD NOT RUN"]


@pytest.mark.asyncio
async def test_drain_discards_buffered_echo() -> None:
    """Frames the mic buffered during playback are drained, not consumed as
    the next command - isolated here with the cooldown disabled."""
    client = FakeServerClient()
    client.sessions = [make_session("s1", title="nightly-refactor")]
    source = DrainableSource(
        # command exchange, then echo buffered during the reply
        [
            WAKE_FRAME,
            SPEECH_FRAME,
            SPEECH_FRAME,
            SILENCE_FRAME,
            SILENCE_FRAME,
            WAKE_FRAME,
            WAKE_FRAME,
            WAKE_FRAME,
        ]
    )
    pipeline, _, _, stt = _pipeline(client, source, ["status"], echo_cooldown_s=0.0)

    await pipeline.start()
    await settle()
    await pipeline.stop()

    assert source.drained == 3  # the buffered echo frames were thrown away
    assert _wakes(client) == 1
    assert len(stt.clips) == 1
