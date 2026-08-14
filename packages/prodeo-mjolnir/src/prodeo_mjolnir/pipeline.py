"""The voice pipeline: mic -> wake word -> VAD -> STT -> intents -> TTS.

One loop owns the microphone and alternates between two modes: scoring
frames for the wake word, and collecting an utterance after a trigger. The
pipeline is half-duplex - it does not listen while speaking - and every
exchange reports its ``voice.*`` events to the server under one
``correlation_id`` so the log tells the whole story.

Attention: a voice exchange marks the user attentive for
``attentive_window_s``; the heartbeat task reports that over
``/api/presence`` (feeding the server's away-only channel suppression), and
the notification speaker uses it to decide whether spoken notifications are
welcome ("attentive" mode) - exactly the routing rule voice-pipeline.md
describes.
"""

import asyncio
import contextlib
import time

import structlog
from ulid import ULID

from prodeo.events import Event
from prodeo.events import types as ev
from prodeo_mjolnir.audio import AudioSink, AudioSource, Drainable, Endpointer
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.client import ServerClient
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.config import MjolnirSettings
from prodeo_mjolnir.engines import (
    AudioClip,
    SpeechToText,
    TextToSpeech,
    WakeWordDetector,
    Warmable,
)
from prodeo_mjolnir.handlers import CommandHandlers, Reply, speakable_name
from prodeo_mjolnir.intents import IntentRouter, Router

_log = structlog.get_logger(__name__)


class VoicePipeline:
    """Owns the listen/speak loop and the attention heartbeat."""

    def __init__(
        self,
        settings: MjolnirSettings,
        *,
        wakeword: WakeWordDetector,
        stt: SpeechToText,
        tts: TextToSpeech,
        source: AudioSource,
        sink: AudioSink,
        client: ServerClient,
        cache: LocalCache,
        handlers: CommandHandlers,
        composer: ResponseComposer,
        router: Router | None = None,
    ) -> None:
        self._settings = settings
        self._wakeword = wakeword
        self._stt = stt
        self._tts = tts
        self._source = source
        self._sink = sink
        self._client = client
        self._cache = cache
        self._handlers = handlers
        self._composer = composer
        self._router = router or IntentRouter()
        self._tasks: list[asyncio.Task[None]] = []
        self._speak_lock = asyncio.Lock()
        self._attentive_until = 0.0
        #: While ``monotonic() < mute_until`` the listen loop drains the mic and
        #: skips wake-word scoring - the echo guard right after Mjölnir speaks.
        self._mute_until = 0.0
        #: While ``monotonic() < followup_until`` the next utterance is captured
        #: without a wake word - Mjölnir just said something inviting a reply
        #: (ADR-0023). Always anchored *after* the echo cooldown.
        self._followup_until = 0.0
        self._notify_queue: asyncio.Queue[Event] = asyncio.Queue()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self._cache.start()
        # Subscribe before the tasks run so no event falls in the gap.
        self._notify_queue = self._cache.subscribe()
        self._tasks = [
            asyncio.create_task(self._listen(), name="mjolnir-listen"),
            asyncio.create_task(self._heartbeat(), name="mjolnir-heartbeat"),
            asyncio.create_task(self._speak_notifications(), name="mjolnir-notify"),
        ]
        # Load the STT model now, in the background, so the *first* command
        # doesn't pay the cold-start; listening starts immediately regardless.
        if isinstance(self._stt, Warmable):
            self._tasks.append(asyncio.create_task(self._warmup(), name="mjolnir-warmup"))
        _log.info(
            "pipeline.started",
            wakeword=self._wakeword.name,
            stt=self._stt.name,
            tts=self._tts.name,
            wake_word=self._settings.wake_word,
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        await self._cache.stop()
        await self._client.forget_presence()

    async def run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.stop()

    # ------------------------------------------------------------ attention

    @property
    def attentive(self) -> bool:
        return time.monotonic() < self._attentive_until

    def _mark_attentive(self) -> None:
        self._attentive_until = time.monotonic() + self._settings.attentive_window_s

    # ----------------------------------------------------------- main loop

    async def _listen(self) -> None:
        endpointer: Endpointer | None = None
        correlation_id = ""
        followup = False
        async for frame in self._source.stream():
            if endpointer is None:
                # Echo guard FIRST: right after speaking, throw away the frames
                # the mic buffered during playback and don't score them for the
                # wake word, so Mjölnir can't hear itself and self-trigger.
                # This must stay ahead of the follow-up check - the window is
                # anchored after the cooldown, and capture must never open
                # while TTS tail can still be in the buffer.
                if time.monotonic() < self._mute_until:
                    self._drain_source()
                    self._wakeword.reset()
                    continue
                if time.monotonic() < self._followup_until:
                    # Mjölnir just asked something: capture the reply directly,
                    # no wake word, no ack, same exchange correlation.
                    self._followup_until = 0.0
                    self._drain_source()
                    self._wakeword.reset()
                    if not correlation_id:
                        correlation_id = str(ULID())
                    followup = True
                    self._mark_attentive()
                    endpointer = self._new_endpointer()
                    continue
                score = self._wakeword.process(frame)
                if score < self._settings.wake_threshold:
                    continue
                self._wakeword.reset()
                correlation_id = str(ULID())
                followup = False
                self._mark_attentive()
                await self._client.post_voice_event(
                    ev.VOICE_WAKE_WORD_DETECTED,
                    {"wake_word": self._settings.wake_word, "score": round(score, 3)},
                    correlation_id=correlation_id,
                )
                await self._client.report_presence(
                    attentive=True, ttl_s=self._settings.presence_ttl_s
                )
                if self._settings.ack_enabled:
                    await self._speak(self._composer.compose("ack"), correlation_id)
                endpointer = self._new_endpointer()
            elif endpointer.add(frame):
                clip, heard = endpointer.clip(), endpointer.heard_speech
                endpointer = None
                await self._handle_utterance(clip, heard, correlation_id, followup=followup)
                followup = False

    async def _warmup(self) -> None:
        """Pre-load the STT model (contained: a failure just means lazy load)."""
        assert isinstance(self._stt, Warmable)
        try:
            await self._stt.warmup()
            _log.info("pipeline.stt_warmed", stt=self._stt.name)
        except Exception:
            _log.warning("pipeline.stt_warmup_failed", stt=self._stt.name, exc_info=True)

    def _new_endpointer(self) -> Endpointer:
        return Endpointer(
            sample_rate=self._settings.sample_rate,
            threshold=self._settings.vad_threshold,
            silence_after_ms=self._settings.vad_silence_ms,
            max_utterance_ms=int(self._settings.max_command_s * 1000),
        )

    async def _handle_utterance(
        self, clip: AudioClip, heard: bool, correlation_id: str, *, followup: bool = False
    ) -> None:
        await self._client.post_voice_event(
            ev.VOICE_COMMAND_RECEIVED,
            {"duration_s": round(clip.duration_s, 2), "heard_speech": heard, "followup": followup},
            correlation_id=correlation_id,
        )
        if not heard:
            # A declined invitation is not an error: silence in the follow-up
            # window just returns to wake-word mode, saying nothing.
            if not followup:
                await self._speak(self._composer.compose("not_heard"), correlation_id)
            return
        try:
            text = await self._stt.transcribe(clip)
        except Exception as exc:
            _log.exception("pipeline.transcription_failed")
            await self._speak(self._composer.compose("error", error=str(exc)), correlation_id)
            return
        await self._client.post_voice_event(
            ev.VOICE_TRANSCRIPTION_COMPLETED,
            {"text": text, "engine": self._stt.name},
            correlation_id=correlation_id,
        )
        if not text.strip():
            if not followup:
                await self._speak(self._composer.compose("not_heard"), correlation_id)
            return
        # A pending clarification dialog consumes the utterance first; when it
        # declines (None), the utterance routes as a fresh intent (ADR-0023).
        if self._handlers.dialog_pending:
            resumed = await self._handlers.resume(text)
            if resumed is not None:
                _log.info("pipeline.dialog_resumed", text=text)
                await self._finish_reply(resumed, correlation_id)
                return
        intent = await self._router.route(text)
        _log.info("pipeline.intent", text=text, intent=type(intent).__name__)
        reply = await self._handlers.handle(intent)
        await self._finish_reply(reply, correlation_id)

    async def _finish_reply(self, reply: Reply, correlation_id: str) -> None:
        self._mark_attentive()
        await self._speak(reply.text, correlation_id)
        if reply.expect_reply:
            self._invite_reply()

    # ------------------------------------------------------------- speaking

    async def _speak(self, text: str, correlation_id: str, session_id: str | None = None) -> None:
        if not text:
            return
        async with self._speak_lock:
            try:
                clip = await self._tts.synthesize(text)
            except Exception:
                _log.exception("pipeline.synthesis_failed", text=text)
                return
            await self._client.post_voice_event(
                ev.VOICE_SPEECH_STARTED,
                {"text": text, "engine": self._tts.name},
                session_id=session_id,
                correlation_id=correlation_id,
            )
            try:
                await self._sink.play(clip)
            finally:
                await self._client.post_voice_event(
                    ev.VOICE_SPEECH_FINISHED,
                    {"duration_s": round(clip.duration_s, 2)},
                    session_id=session_id,
                    correlation_id=correlation_id,
                )
            self._suppress_echo()

    def _drain_source(self) -> None:
        """Discard mic frames buffered during playback, if the source can."""
        if isinstance(self._source, Drainable):
            self._source.drain()

    def _suppress_echo(self) -> None:
        """Post-speech echo guard: drop buffered frames, forget any partial
        wake-word evidence, and mute wake scoring for ``echo_cooldown_s``."""
        self._drain_source()
        self._wakeword.reset()
        self._mute_until = time.monotonic() + self._settings.echo_cooldown_s

    def _invite_reply(self) -> None:
        """Open the no-wake follow-up window (ADR-0023).

        Anchored after ``mute_until`` so the echo guard always wins: capture
        can never open while TTS tail may still be in the mic buffer.
        """
        base = max(time.monotonic(), self._mute_until)
        self._followup_until = base + self._settings.followup_window_s

    # -------------------------------------------------------- notifications

    async def _heartbeat(self) -> None:
        while True:
            await self._client.report_presence(
                attentive=self.attentive, ttl_s=self._settings.presence_ttl_s
            )
            await asyncio.sleep(self._settings.heartbeat_interval_s)

    async def _speak_notifications(self) -> None:
        while True:
            event = await self._notify_queue.get()
            mode = self._settings.speak_notifications
            if mode == "never" or (mode == "attentive" and not self.attentive):
                continue
            spoken = self._notification_text(event)
            if not spoken:
                continue
            await self._speak(spoken, str(ULID()), session_id=event.session_id)
            if event.type == ev.INTERACTION_REQUESTED:
                # The announcement invites an answer: remember which
                # interaction was read out, then listen without a wake word.
                interaction = event.payload.get("interaction", {})
                interaction_id = str(interaction.get("id", ""))
                if interaction_id:
                    self._handlers.note_announced_interaction(interaction_id)
                self._invite_reply()

    def _notification_text(self, event: Event) -> str:
        session = self._cache.session(event.session_id or "")
        if event.type == ev.INTERACTION_REQUESTED:
            interaction = event.payload.get("interaction", {})
            return self._composer.compose(
                "notify_interaction",
                adapter=str(interaction.get("adapter", "an agent")),
                name=speakable_name(session),
                title=str(interaction.get("title", "it needs your attention")),
            )
        if event.type == ev.SESSION_COMPLETED:
            return self._composer.compose("notify_completed", name=speakable_name(session))
        if event.type == ev.SESSION_FAILED:
            return self._composer.compose("notify_failed", name=speakable_name(session))
        return ""
