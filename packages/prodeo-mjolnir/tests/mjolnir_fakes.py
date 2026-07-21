"""Shared fakes: a scriptable server client and audio/engine stand-ins."""

import array
import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from prodeo.events import Event, new_event
from prodeo.mediation import Interaction, InteractionKind
from prodeo.sessions import Session, SessionState
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.client import ServerClient
from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip
from prodeo_mjolnir.errors import AlreadyResolvedError

FRAME_MS = 80
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def frame(amplitude: int) -> bytes:
    samples = array.array(
        "h", [amplitude if i % 2 == 0 else -amplitude for i in range(FRAME_SAMPLES)]
    )
    return samples.tobytes()


SPEECH_FRAME = frame(2000)
SILENCE_FRAME = frame(0)
#: Recognized by FakeWakeWord, inaudible to the Endpointer's VAD.
WAKE_FRAME = frame(1)


def make_session(
    id_: str,
    *,
    project: str = "",
    title: str = "",
    adapter: str = "claude-code",
    state: SessionState = SessionState.RUNNING,
    active_ago_s: float = 60.0,
) -> Session:
    now = datetime.now(UTC)
    return Session(
        id=id_,
        adapter=adapter,
        native_id=f"native-{id_}",
        title=title,
        project=project,
        state=state,
        created_at=now - timedelta(hours=1),
        last_activity_at=now - timedelta(seconds=active_ago_s),
    )


def make_interaction(
    id_: str,
    session_id: str,
    *,
    title: str,
    adapter: str = "claude-code",
    body: str = "",
) -> Interaction:
    return Interaction(
        id=id_,
        session_id=session_id,
        adapter=adapter,
        native_id=f"tool-{id_}",
        kind=InteractionKind.PERMISSION,
        title=title,
        body=body,
        requested_at=datetime.now(UTC),
    )


class FakeServerClient:
    """Duck-typed ServerClient: scripted snapshots, pushable event stream."""

    def __init__(self) -> None:
        self.client_id = "mjolnir-test"
        self.sessions: list[Session] = []
        self.interactions: list[Interaction] = []
        self.already_resolved: set[str] = set()
        self.answered: list[tuple[str, str | None]] = []
        self.terminated: list[str] = []
        self.voice_events: list[Event] = []
        self.presence_reports: list[bool] = []
        self.presence_forgotten = False
        self._stream: asyncio.Queue[Event] = asyncio.Queue()

    # -- snapshots

    async def list_sessions(self) -> list[Session]:
        return list(self.sessions)

    async def list_pending_interactions(self) -> list[Interaction]:
        return list(self.interactions)

    # -- commands

    async def answer(
        self,
        interaction_id: str,
        *,
        decision: Literal["allow", "deny"] | None = None,
        text: str = "",
    ) -> Interaction:
        if interaction_id in self.already_resolved:
            raise AlreadyResolvedError(interaction_id)
        self.answered.append((interaction_id, decision))
        return next(i for i in self.interactions if i.id == interaction_id)

    async def terminate(self, session_id: str) -> None:
        self.terminated.append(session_id)

    # -- reporting

    async def post_voice_event(
        self,
        type_: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.voice_events.append(
            new_event(
                type_,
                payload=payload or {},
                session_id=session_id,
                correlation_id=correlation_id,
                source=f"voice:{self.client_id}",
            )
        )

    async def report_presence(self, *, attentive: bool, ttl_s: float) -> None:
        self.presence_reports.append(attentive)

    async def forget_presence(self) -> None:
        self.presence_forgotten = True

    # -- stream

    async def stream_events(self, types: list[str]) -> AsyncIterator[Event]:
        while True:
            yield await self._stream.get()

    def push(self, event: Event) -> None:
        self._stream.put_nowait(event)

    def as_client(self) -> ServerClient:
        """The cast every consumer constructor wants."""
        return cast("ServerClient", self)


async def started_cache(client: FakeServerClient) -> LocalCache:
    cache = LocalCache(client.as_client())
    await cache.start()
    return cache


async def settle() -> None:
    """Let queued events propagate through the cache task."""
    for _ in range(10):
        await asyncio.sleep(0.005)


class FakeWakeWord:
    def __init__(self) -> None:
        self.resets = 0

    @property
    def name(self) -> str:
        return "fake-wake"

    def process(self, frame_: bytes) -> float:
        return 1.0 if frame_ == WAKE_FRAME else 0.0

    def reset(self) -> None:
        self.resets += 1


class FakeStt:
    """Returns scripted transcripts, one per captured utterance.

    Implements the optional ``Warmable`` capability (a no-op counter) so the
    pipeline's startup pre-warm is exercised without consuming a transcript.
    """

    def __init__(self, transcripts: list[str]) -> None:
        self.transcripts = list(transcripts)
        self.clips: list[AudioClip] = []
        self.warmups = 0

    @property
    def name(self) -> str:
        return "fake-stt"

    async def transcribe(self, clip: AudioClip) -> str:
        self.clips.append(clip)
        return self.transcripts.pop(0) if self.transcripts else ""

    async def warmup(self) -> None:
        self.warmups += 1


class FakeTts:
    def __init__(self) -> None:
        self.texts: list[str] = []

    @property
    def name(self) -> str:
        return "fake-tts"

    async def synthesize(self, text: str) -> AudioClip:
        self.texts.append(text)
        return AudioClip(pcm=b"\x00\x00" * 160, sample_rate=SAMPLE_RATE)


class FakeSink:
    def __init__(self) -> None:
        self.played: list[AudioClip] = []

    async def play(self, clip: AudioClip) -> None:
        self.played.append(clip)


class ScriptedSource:
    """Yields a scripted frame sequence, then ends the stream."""

    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    async def stream(self) -> AsyncIterator[bytes]:
        for item in self.frames:
            yield item
            await asyncio.sleep(0)
