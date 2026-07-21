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
from prodeo_mjolnir.audio import AudioSink, AudioSource, Endpointer
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
from prodeo_mjolnir.handlers import CommandHandlers, speakable_name
from prodeo_mjolnir.intents import IntentRouter

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
        router: IntentRouter | None = None,
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
        async for frame in self._source.stream():
            if endpointer is None:
                score = self._wakeword.process(frame)
                if score < self._settings.wake_threshold:
                    continue
                self._wakeword.reset()
                correlation_id = str(ULID())
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
                await self._handle_utterance(clip, heard, correlation_id)

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

    async def _handle_utterance(self, clip: AudioClip, heard: bool, correlation_id: str) -> None:
        await self._client.post_voice_event(
            ev.VOICE_COMMAND_RECEIVED,
            {"duration_s": round(clip.duration_s, 2), "heard_speech": heard},
            correlation_id=correlation_id,
        )
        if not heard:
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
            await self._speak(self._composer.compose("not_heard"), correlation_id)
            return
        intent = self._router.route(text)
        _log.info("pipeline.intent", text=text, intent=type(intent).__name__)
        response = await self._handlers.handle(intent)
        self._mark_attentive()
        await self._speak(response, correlation_id)

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
            if spoken:
                await self._speak(spoken, str(ULID()), session_id=event.session_id)

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
