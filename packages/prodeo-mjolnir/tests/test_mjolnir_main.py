"""Composition-root wiring: default LLM brain, identity linking, router build."""

from prodeo_mjolnir.config import MjolnirSettings
from prodeo_mjolnir.intents import IntentRouter
from prodeo_mjolnir.llm_router import LlmIntentRouter
from prodeo_mjolnir.main import _build_router, _link_llm_identity


def _settings(**overrides: object) -> MjolnirSettings:
    return MjolnirSettings(**overrides)  # type: ignore[arg-type]  # test-only kwargs


def test_defaults_make_ollama_the_brain() -> None:
    s = MjolnirSettings()
    assert s.intent_router == "llm"
    assert s.persona_rephraser == "ollama"
    # 127.0.0.1, not localhost: Ollama is IPv4-only and the ::1 attempt that
    # `localhost` tries first costs ~2s per call before it falls back.
    assert s.llm_base_url == "http://127.0.0.1:11434"
    assert s.llm_model == "llama3.1:8b"
    # actions are classifiable by the LLM by default (ADR-0013)
    assert {"approve", "deny", "stop"} <= set(s.llm_intents)


def test_link_llm_identity_feeds_rephraser_from_canonical() -> None:
    s = _settings(persona_rephraser="ollama", llm_base_url="http://gpu:11434", llm_model="foo")
    linked = _link_llm_identity(s)
    assert linked.engines["ollama"] == {"base_url": "http://gpu:11434", "model": "foo"}


def test_link_llm_identity_respects_explicit_engine_override() -> None:
    s = _settings(
        persona_rephraser="ollama",
        llm_model="foo",
        engines={"ollama": {"model": "bar", "options": {"temperature": 0.1}}},
    )
    linked = _link_llm_identity(s)
    # explicit MJOLNIR_ENGINES wins for model; base_url still filled from canonical
    assert linked.engines["ollama"]["model"] == "bar"
    assert linked.engines["ollama"]["base_url"] == "http://127.0.0.1:11434"
    assert linked.engines["ollama"]["options"] == {"temperature": 0.1}


def test_link_llm_identity_keyed_on_plugin_name_not_ollama() -> None:
    # A future backend rename is pure config: linking follows persona_rephraser.
    s = _settings(persona_rephraser="stormbreaker", llm_model="foo")
    linked = _link_llm_identity(s)
    assert linked.engines["stormbreaker"]["model"] == "foo"
    assert "ollama" not in linked.engines


def test_link_llm_identity_noop_when_rephraser_disabled() -> None:
    s = _settings(persona_rephraser="", llm_model="foo")
    assert _link_llm_identity(s).engines == {}


def test_build_router_uses_canonical_identity_and_actions() -> None:
    s = _settings(intent_router="llm", llm_base_url="http://gpu:11434/", llm_model="foo")
    router = _build_router(s)
    assert isinstance(router, LlmIntentRouter)
    assert router._base_url == "http://gpu:11434"  # rstrip'd
    assert router._model == "foo"
    assert {"approve", "deny", "stop"} <= router._allowed


def test_build_router_patterns_mode_is_deterministic_only() -> None:
    router = _build_router(_settings(intent_router="patterns"))
    assert isinstance(router, IntentRouter)
