"""App manifest resolution and the config-to-environment contract."""

from typing import Any

import pytest
from pydantic import BaseModel

from prodeo.apps import APP_API_VERSION, AppManifest, config_to_env, installed_apps
from prodeo.apps.manifest import resolve_app_manifest


class DemoSettings(BaseModel):
    server_url: str = ""
    api_token: str = ""


class FakeEntryPoint:
    def __init__(self, name: str, obj: Any) -> None:
        self.name = name
        self._obj = obj

    def load(self) -> Any:
        return self._obj


def _manifest(**overrides: Any) -> AppManifest:
    kwargs: dict[str, Any] = {
        "name": "demo",
        "version": "1.0",
        "command": ["demo-client"],
        "env_prefix": "DEMO_",
    }
    kwargs.update(overrides)
    return AppManifest(**kwargs)


def test_entry_point_may_be_a_manifest_or_a_factory() -> None:
    direct = _manifest()
    assert resolve_app_manifest(FakeEntryPoint("demo", direct)).name == "demo"
    assert resolve_app_manifest(FakeEntryPoint("demo", lambda: direct)).name == "demo"


def test_wrong_product_is_refused() -> None:
    with pytest.raises(TypeError, match="AppManifest"):
        resolve_app_manifest(FakeEntryPoint("demo", lambda: object()))


def test_api_version_mismatch_is_refused() -> None:
    stale = _manifest(app_api_version=APP_API_VERSION + 1)
    with pytest.raises(RuntimeError, match="version mismatch"):
        resolve_app_manifest(FakeEntryPoint("demo", stale))


def test_a_broken_app_is_skipped_not_fatal() -> None:
    def explodes() -> AppManifest:
        raise ImportError("missing dependency")

    found = installed_apps(
        lambda: [FakeEntryPoint("boom", explodes), FakeEntryPoint("ok", _manifest())]
    )
    # An app that cannot be described is a degraded dashboard, never a reason
    # to fail the server's boot.
    assert [m.name for m in found] == ["demo"]


def test_config_becomes_prefixed_upper_case_env() -> None:
    env = config_to_env(_manifest(), {"api_token": "secret", "wake_word": "mjölnir"})
    assert env == {"DEMO_API_TOKEN": "secret", "DEMO_WAKE_WORD": "mjölnir"}


def test_non_strings_are_json_encoded_for_pydantic_settings() -> None:
    env = config_to_env(
        _manifest(),
        {"engines": {"piper": {"voice_path": "/v.onnx"}}, "beam_size": 5, "ack_enabled": False},
    )
    # pydantic-settings parses complex and scalar non-string values as JSON, so
    # this is exactly what it expects to read back.
    assert env["DEMO_ENGINES"] == '{"piper": {"voice_path": "/v.onnx"}}'
    assert env["DEMO_BEAM_SIZE"] == "5"
    assert env["DEMO_ACK_ENABLED"] == "false"


def test_strings_pass_through_unquoted() -> None:
    # Quoting a string would make pydantic-settings hand the app a value with
    # literal quote characters in it.
    assert config_to_env(_manifest(), {"honorific": "sir"})["DEMO_HONORIFIC"] == '"sir"'[1:-1]


def test_no_prefix_means_no_environment() -> None:
    assert config_to_env(_manifest(env_prefix=""), {"anything": "x"}) == {}
