"""Phase 4 exit criterion: the vision.md morning scenario, end to end, offline.

A real composed server (ephemeral port), a real Mjölnir pipeline talking to
it over actual HTTP + WebSocket - only the hardware seams (mic, speaker) and
model engines (wake word, STT, TTS) are fakes, which is exactly what the
engine Protocols exist for. The user says "good morning", hears about the
three overnight agents (one finished, one failed, one blocked on a
permission), says "approve it", and the interaction resolves exactly once.
"""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from prodeo.config import Settings
from prodeo.mediation import Answer, Interaction, InteractionKind, InteractionRequest
from prodeo.server import Server
from prodeo.sessions import SessionDescriptor, SessionState
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.client import ServerClient
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.config import MjolnirSettings
from prodeo_mjolnir.errors import AlreadyResolvedError
from prodeo_mjolnir.handlers import CommandHandlers
from prodeo_mjolnir.packs import NEUTRAL
from prodeo_mjolnir.pipeline import VoicePipeline

sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "prodeo-mjolnir" / "tests"))
from mjolnir_fakes import (
    SILENCE_FRAME,
    SPEECH_FRAME,
    WAKE_FRAME,
    FakeSink,
    FakeStt,
    FakeTts,
    FakeWakeWord,
    ScriptedSource,
)

pytestmark = pytest.mark.integration

TOKEN = "voice-test-token"


async def _seed_overnight(server: Server) -> Interaction:
    """Three overnight agents: finished, failed, blocked on a permission."""
    registry = server.registry
    s1 = await registry.upsert_discovered(
        "claude-code", SessionDescriptor(native_id="n1", title="nightly-refactor")
    )
    await registry.observe_state(s1.id, SessionState.COMPLETED, reason="done")
    s2 = await registry.upsert_discovered(
        "claude-code", SessionDescriptor(native_id="n2", project="/repos/api-tests")
    )
    await registry.observe_state(s2.id, SessionState.FAILED, reason="test regression")
    s3 = await registry.upsert_discovered(
        "claude-code", SessionDescriptor(native_id="n3", project="/repos/db-migration")
    )
    await registry.observe_state(s3.id, SessionState.WAITING_ON_USER, reason="permission")

    delivered: list[Answer] = []

    async def deliver(interaction: Interaction, answer: Answer) -> None:
        delivered.append(answer)

    interaction = await server.mediation.open(
        InteractionRequest(
            session_id=s3.id,
            adapter="claude-code",
            native_id="tool-1",
            kind=InteractionKind.PERMISSION,
            title="May I run the database migration?",
        ),
        deliver,
    )
    return interaction


@pytest.mark.asyncio
async def test_morning_scenario_end_to_end(tmp_path: Path) -> None:
    settings = Settings(
        node_name="hub",
        data_dir=tmp_path,
        api_port=0,
        api_token=TOKEN,
        adapters={"claude-code": {"projects_dir": str(tmp_path / "no-projects")}},
        discovery_interval_s=0,
        dashboard_dir=tmp_path / "no-dashboard",
    )
    server = Server(settings)
    await server.start()
    try:
        interaction = await _seed_overnight(server)
        base_url = f"http://127.0.0.1:{server.api.port}"

        # --- a real Mjölnir client against the real API
        mj_settings = MjolnirSettings(
            server_url=base_url,
            api_token=TOKEN,
            node="kitchen-pi",
            ack_enabled=False,
            vad_silence_ms=160,
            heartbeat_interval_s=0.05,
        )
        client = ServerClient(base_url, api_token=TOKEN, client_id="mjolnir", node="kitchen-pi")
        cache = LocalCache(client)
        composer = ResponseComposer(NEUTRAL, honorific="sir")
        handlers = CommandHandlers(cache, client, composer)
        tts = FakeTts()
        sink = FakeSink()
        exchange = [WAKE_FRAME, SPEECH_FRAME, SPEECH_FRAME, SILENCE_FRAME, SILENCE_FRAME]
        pipeline = VoicePipeline(
            mj_settings,
            wakeword=FakeWakeWord(),
            stt=FakeStt(["good morning", "approve it"]),
            tts=tts,
            source=ScriptedSource(exchange * 2),
            sink=sink,
            client=client,
            cache=cache,
            handlers=handlers,
            composer=composer,
        )
        await pipeline.start()

        async with asyncio.timeout(10):
            while len(tts.texts) < 2:
                await asyncio.sleep(0.02)

        # --- the briefing covered all three overnight agents
        briefing = tts.texts[0]
        assert "3 agent sessions ran while you were away, sir." in briefing
        assert "nightly-refactor finished." in briefing
        assert "api-tests failed." in briefing
        assert "db-migration is waiting on you: May I run the database migration?" in briefing

        # --- "approve it" resolved the permission, spoken back deterministically
        assert tts.texts[1] == "Approved, sir."
        resolved = server.mediation.get(interaction.id)
        assert resolved is not None and resolved.status.value == "answered"
        assert resolved.answer is not None and resolved.answer.decision == "allow"

        # --- exactly-once: a second answer (simultaneous dashboard click) loses
        with pytest.raises(AlreadyResolvedError):
            await client.answer(interaction.id, decision="deny")

        # --- the satellite is present and attentive on the hub
        async with httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {TOKEN}"}
        ) as http:
            presence = (await http.get("/api/presence")).json()
            assert presence["any_attentive"] is True
            assert presence["clients"][0]["client_id"] == "mjolnir"
            assert presence["clients"][0]["node"] == "kitchen-pi"

            # --- every voice.* event landed in the unified log
            async with asyncio.timeout(10):
                while True:
                    body = (await http.get("/api/events", params={"type": "voice.*"})).json()
                    if len(body["events"]) >= 10:  # 5 per exchange
                        break
                    await asyncio.sleep(0.05)
        by_type = [e["type"] for e in body["events"]]
        assert by_type.count("voice.wake_word_detected") == 2
        assert by_type.count("voice.transcription_completed") == 2
        assert all(e["source"] == "voice:mjolnir" for e in body["events"])
        assert all(e["node"] == "kitchen-pi" for e in body["events"])
        transcripts = [
            e["payload"]["text"]
            for e in body["events"]
            if e["type"] == "voice.transcription_completed"
        ]
        assert transcripts == ["good morning", "approve it"]

        await pipeline.stop()
        await client.close()
    finally:
        await server.stop()
