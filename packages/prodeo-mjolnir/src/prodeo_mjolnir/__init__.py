"""Mjölnir: the Prodeo Command Center voice client.

A client, not a subsystem: it talks to the server over the same REST +
WebSocket API as the dashboard, and may run on separate hardware (a
Raspberry Pi satellite). See docs/architecture/voice-pipeline.md.
"""

from prodeo_mjolnir.audio import AudioSink, AudioSource, Endpointer
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.client import ServerClient
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.config import MjolnirSettings
from prodeo_mjolnir.engines import (
    SAMPLE_RATE,
    AudioClip,
    SpeechToText,
    TextToSpeech,
    WakeWordDetector,
)
from prodeo_mjolnir.errors import AlreadyResolvedError, EngineNotFoundError, MjolnirError
from prodeo_mjolnir.handlers import CommandHandlers
from prodeo_mjolnir.intents import IntentRouter
from prodeo_mjolnir.packs import BUILTIN_PACKS, load_pack
from prodeo_mjolnir.pipeline import VoicePipeline
from prodeo_mjolnir.plugins import Engines, load_engines

VERSION = "0.1.0"

__all__ = [
    "BUILTIN_PACKS",
    "SAMPLE_RATE",
    "VERSION",
    "AlreadyResolvedError",
    "AudioClip",
    "AudioSink",
    "AudioSource",
    "CommandHandlers",
    "Endpointer",
    "EngineNotFoundError",
    "Engines",
    "IntentRouter",
    "LocalCache",
    "MjolnirError",
    "MjolnirSettings",
    "ResponseComposer",
    "ServerClient",
    "SpeechToText",
    "TextToSpeech",
    "VoicePipeline",
    "WakeWordDetector",
    "load_engines",
    "load_pack",
]
