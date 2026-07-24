"""Voice client configuration via Pydantic Settings (prefix ``MJOLNIR_``)."""

from pathlib import Path
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from prodeo_mjolnir.engines import SAMPLE_RATE


class MjolnirSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MJOLNIR_")

    log_level: str = "INFO"

    # Server connection (same REST + WebSocket API the dashboard uses)
    server_url: str = "http://127.0.0.1:8600"
    api_token: str = ""
    #: How this client identifies itself (presence, ``voice:<client_id>``).
    client_id: str = "mjolnir"
    #: Node name stamped on reported voice events (the satellite's machine
    #: name, e.g. ``kitchen-pi``). Empty = the server's own node name.
    node: str = ""

    # Engines (plugin names in the ``prodeo.plugins`` entry-point group)
    wakeword_plugin: str = "openwakeword"
    stt_plugin: str = "faster-whisper"
    tts_plugin: str = "piper"
    #: Per-engine config, keyed by plugin name. From the environment this is
    #: JSON, e.g. ``MJOLNIR_ENGINES='{"piper": {"voice": "en_GB-alan-medium"}}'``.
    engines: dict[str, dict[str, Any]] = {}
    #: The wake word model the detector listens for. The default is the
    #: client's own name, spoken the Norse way ("MYOL-neer"); any other
    #: OpenWakeWord model name/path works - nothing hard-codes the default.
    wake_word: str = "mjölnir"
    wake_threshold: float = 0.5
    #: Spoken acknowledgement after the wake word (the ``ack`` template);
    #: disable for a silent (chime-less) satellite.
    ack_enabled: bool = True

    # Audio capture
    sample_rate: int = SAMPLE_RATE
    frame_ms: int = 80
    #: Per-frame loudness (RMS) above which a frame counts as speech. This is
    #: mic- and room-specific; run ``prodeo-mjolnir --calibrate`` to find yours.
    vad_threshold: float = 300.0
    vad_silence_ms: int = 1000
    max_command_s: float = 12.0
    #: After Mjölnir speaks, discard mic frames buffered during playback and
    #: refuse to score the wake word for this long, so TTS bleeding
    #: speaker->mic can't self-trigger (half-duplex echo guard).
    echo_cooldown_s: float = 0.4

    # Intent routing
    #: ``patterns`` = deterministic offline grammar only (default). ``llm`` adds
    #: a constrained Ollama classifier consulted *only* when the grammar returns
    #: UnknownIntent (see docs/adr/0012-llm-intent-router.md).
    intent_router: Literal["patterns", "llm"] = "patterns"
    llm_router_base_url: str = "http://localhost:11434"
    llm_router_model: str = "llama3.2"
    #: Bound on one classification call; on timeout the utterance is UnknownIntent.
    llm_router_timeout_s: float = 4.0
    #: The closed set of intents the LLM may emit. Defaults to read-only
    #: intents; add ``approve``/``deny``/``stop`` to let it classify actions.
    llm_intents: list[str] = ["status", "pending", "overnight", "help", "cancel"]

    # Persona (see docs/architecture/voice-pipeline.md#persona)
    #: Interpolated into every response template ("sir", "ma'am", a name, or
    #: empty for none).
    honorific: str = ""
    #: Built-in template pack name (``neutral`` or ``steward``).
    persona_pack: str = "neutral"
    #: Optional JSON file of template overrides layered on the pack.
    persona_pack_file: Path | None = None
    #: Summarizer-kind plugin (e.g. ``ollama``) that rephrases
    #: *non-time-critical* responses - the overnight briefing - in persona.
    #: Confirmations stay deterministic templates regardless. Empty = off.
    persona_rephraser: str = ""
    #: Bound on rephrasing; on timeout the deterministic text is spoken.
    rephrase_timeout_s: float = 10.0

    # Attention + notification speaking
    #: Speak server notifications: only while this client is attentive
    #: (default), always, or never.
    speak_notifications: Literal["attentive", "always", "never"] = "attentive"
    #: How long after a voice exchange the user still counts as attentive.
    attentive_window_s: float = 120.0
    presence_ttl_s: float = 30.0
    heartbeat_interval_s: float = 10.0

    # Queries
    #: Lookback window for "what happened overnight".
    overnight_hours: float = 12.0

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000
