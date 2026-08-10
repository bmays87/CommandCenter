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

    # LLM brain (Mjölnir's personality; Ollama by default). One identity powers
    # both the intent router and the persona rephraser - see
    # docs/adr/0013-ollama-default-brain.md. Swap the backend by pointing these
    # at another endpoint/model and setting ``persona_rephraser`` to its plugin.
    #: Ollama (or compatible) endpoint both the router and rephraser use.
    #: 127.0.0.1 rather than ``localhost`` on purpose: Ollama binds IPv4 only,
    #: while ``localhost`` resolves to ``::1`` first, and the failed IPv6
    #: attempt costs ~2s *per call* before the fallback - a third of the
    #: router's budget spent on nothing. Override for a remote backend.
    llm_base_url: str = "http://127.0.0.1:11434"
    #: The model both the router and rephraser use.
    llm_model: str = "llama3.1:8b"

    # Intent routing
    #: ``llm`` (default) = constrained LLM classifier consulted *only* when the
    #: deterministic grammar returns UnknownIntent; ``patterns`` = grammar only,
    #: fully offline (docs/adr/0012-llm-intent-router.md).
    intent_router: Literal["patterns", "llm"] = "llm"
    #: Bound on one classification call; on timeout the utterance is UnknownIntent.
    llm_router_timeout_s: float = 4.0
    #: The closed set of intents the LLM may emit. Actions (``approve``/``deny``/
    #: ``stop``) are included by default; the handler still resolves the real
    #: target and guards against ambiguous matches (ADR-0013).
    llm_intents: list[str] = [
        "status",
        "pending",
        "overnight",
        "help",
        "cancel",
        "approve",
        "deny",
        "stop",
    ]

    # Question answering (ADR-0018)
    #: ``llm`` (default) = utterances no intent matched become grounded
    #: questions to the LLM brain, answered from a live state snapshot;
    #: ``off`` = the deterministic "didn't understand" template, as before.
    question_answering: Literal["llm", "off"] = "llm"
    #: Bound on one answer; on timeout the "didn't understand" template speaks.
    qa_timeout_s: float = 10.0

    # Persona (see docs/architecture/voice-pipeline.md#persona)
    #: Interpolated into every response template ("sir", "ma'am", a name, or
    #: empty for none).
    honorific: str = ""
    #: Built-in template pack name (``neutral`` or ``steward``).
    persona_pack: str = "neutral"
    #: Optional JSON file of template overrides layered on the pack.
    persona_pack_file: Path | None = None
    #: Summarizer-kind plugin that rephrases *non-time-critical* responses - the
    #: overnight briefing - in persona, using the ``llm_*`` identity above.
    #: Confirmations stay deterministic templates regardless. Empty = off.
    persona_rephraser: str = "ollama"
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
