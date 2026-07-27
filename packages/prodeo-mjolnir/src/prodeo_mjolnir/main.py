"""Composition root and console entry point: ``prodeo-mjolnir``.

The only module where concrete implementations are wired (mirroring the
server's ``server.py`` rule): engines from plugins, PortAudio I/O, the
server client, cache, handlers, composer, pipeline.
"""

import argparse
import asyncio
import contextlib
import signal

import httpx
import structlog

from prodeo.logging import configure_logging
from prodeo_mjolnir.audio import SoundDeviceSink, SoundDeviceSource
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.calibrate import run_calibration
from prodeo_mjolnir.client import ServerClient
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.config import MjolnirSettings
from prodeo_mjolnir.handlers import CommandHandlers
from prodeo_mjolnir.intents import IntentRouter, Router
from prodeo_mjolnir.llm_router import LlmIntentRouter
from prodeo_mjolnir.packs import load_pack
from prodeo_mjolnir.pipeline import VoicePipeline
from prodeo_mjolnir.plugins import load_engines

_log = structlog.get_logger(__name__)


def build_pipeline(settings: MjolnirSettings) -> tuple[VoicePipeline, ServerClient]:
    """Wire everything; separated from ``run`` for reuse and inspection."""
    settings = _link_llm_identity(settings)
    engines = load_engines(settings)
    client = ServerClient(
        settings.server_url,
        api_token=settings.api_token,
        client_id=settings.client_id,
        node=settings.node,
    )
    cache = LocalCache(client)
    rephraser = engines.rephrasers.get(settings.persona_rephraser)
    composer = ResponseComposer(
        load_pack(settings.persona_pack, settings.persona_pack_file),
        honorific=settings.honorific,
        rephraser=rephraser,
        rephrase_timeout_s=settings.rephrase_timeout_s,
    )
    handlers = CommandHandlers(cache, client, composer, overnight_hours=settings.overnight_hours)
    pipeline = VoicePipeline(
        settings,
        wakeword=engines.wakeword,
        stt=engines.stt,
        tts=engines.tts,
        source=SoundDeviceSource(sample_rate=settings.sample_rate, frame_ms=settings.frame_ms),
        sink=SoundDeviceSink(),
        client=client,
        cache=cache,
        handlers=handlers,
        composer=composer,
        router=_build_router(settings),
    )
    return pipeline, client


def _link_llm_identity(settings: MjolnirSettings) -> MjolnirSettings:
    """Make Mjölnir's ``llm_*`` identity the default for the persona rephraser.

    The intent router reads ``llm_base_url``/``llm_model`` directly; the
    rephraser is a summarizer plugin configured through ``engines[<name>]``.
    Fold the canonical identity into that plugin's config so one knob drives
    both - an explicit ``MJOLNIR_ENGINES`` entry still wins. Keyed on the
    plugin name (not the literal ``"ollama"``) so swapping the backend is pure
    config, keeping this the only place the two are linked.
    """
    name = settings.persona_rephraser
    if not name:
        return settings
    merged = {
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        **settings.engines.get(name, {}),
    }
    return settings.model_copy(update={"engines": {**settings.engines, name: merged}})


def _build_router(settings: MjolnirSettings) -> Router:
    """Deterministic grammar first; the constrained LLM fallback when enabled."""
    base = IntentRouter()
    if settings.intent_router != "llm":
        return base
    return LlmIntentRouter(
        base=base,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        allowed=set(settings.llm_intents),
        timeout_s=settings.llm_router_timeout_s,
    )


async def run(settings: MjolnirSettings | None = None) -> None:
    settings = settings or MjolnirSettings()
    configure_logging(settings.log_level)
    pipeline, client = build_pipeline(settings)
    await _probe_llm(settings)
    await pipeline.start()
    _log.info("mjolnir.started", server=settings.server_url, client_id=settings.client_id)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # non-POSIX platforms
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await pipeline.stop()
        await client.close()
        _log.info("mjolnir.stopped")


async def _probe_llm(settings: MjolnirSettings) -> None:
    """Warn (non-fatally) if Mjölnir's brain is expected but unreachable.

    The router fails closed and the grammar still works, so this is only a
    heads-up that the LLM understanding/personality will be absent.
    """
    if settings.intent_router != "llm" and not settings.persona_rephraser:
        return
    try:
        async with httpx.AsyncClient(base_url=settings.llm_base_url, timeout=2.0) as http:
            (await http.get("/api/tags")).raise_for_status()
    except Exception as exc:
        _log.warning("mjolnir.llm_unreachable", url=settings.llm_base_url, error=str(exc))


async def calibrate(settings: MjolnirSettings | None = None) -> None:
    """Measure the microphone and recommend ``MJOLNIR_VAD_THRESHOLD``."""
    settings = settings or MjolnirSettings()
    configure_logging(settings.log_level)
    source = SoundDeviceSource(sample_rate=settings.sample_rate, frame_ms=settings.frame_ms)
    await run_calibration(source)


def main() -> None:
    """Console entry point: ``prodeo-mjolnir``."""
    parser = argparse.ArgumentParser(prog="prodeo-mjolnir", description="Prodeo voice client")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="measure the mic and recommend MJOLNIR_VAD_THRESHOLD, then exit "
        "(use this if Mjolnir keeps saying 'I didn't catch that' or is slow to respond)",
    )
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(calibrate() if args.calibrate else run())
