"""Response Composer + persona packs: honorific slot, overrides, rephrasing."""

import json
from pathlib import Path

import pytest

from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.errors import MjolnirError
from prodeo_mjolnir.packs import BUILTIN_PACKS, NEUTRAL, STEWARD, load_pack


def test_every_pack_covers_every_key() -> None:
    for name, pack in BUILTIN_PACKS.items():
        assert set(pack) == set(NEUTRAL), name


def test_honorific_renders_or_disappears() -> None:
    plain = ResponseComposer(NEUTRAL)
    sir = ResponseComposer(NEUTRAL, honorific="sir")
    assert plain.compose("approved") == "Approved."
    assert sir.compose("approved") == "Approved, sir."
    assert sir.compose("ack") == "Yes, sir?"


def test_count_pluralization() -> None:
    composer = ResponseComposer(NEUTRAL)
    assert composer.compose("status_active", count=1, sessions="x") == "1 session active: x."
    assert composer.compose("status_active", count=3, sessions="x, y and z").startswith(
        "3 sessions active"
    )


def test_steward_pack_restyles_deterministically() -> None:
    composer = ResponseComposer(STEWARD, honorific="sir")
    assert composer.compose("approved") == "As you wish, sir. The permission has been granted."
    assert composer.compose("stopped", name="nightly") == (
        "As you wish, sir. nightly has been terminated."
    )


def test_pack_file_overrides_and_validation(tmp_path: Path) -> None:
    override = tmp_path / "pack.json"
    override.write_text(json.dumps({"ack": "Speak{honorific}."}), encoding="utf-8")
    pack = load_pack("neutral", override)
    assert pack["ack"] == "Speak{honorific}."
    assert pack["approved"] == NEUTRAL["approved"]

    bad_keys = tmp_path / "bad.json"
    bad_keys.write_text(json.dumps({"nonsense": "x"}), encoding="utf-8")
    with pytest.raises(MjolnirError, match="unknown keys"):
        load_pack("neutral", bad_keys)

    with pytest.raises(MjolnirError, match="unknown persona pack"):
        load_pack("hal9000")


class StubRephraser:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "stub"

    async def summarize(self, instructions: str, content: str) -> str:
        self.calls.append((instructions, content))
        return self.reply


class ExplodingRephraser:
    @property
    def name(self) -> str:
        return "boom"

    async def summarize(self, instructions: str, content: str) -> str:
        raise RuntimeError("model offline")


@pytest.mark.asyncio
async def test_rephrase_uses_plugin_and_survives_failure() -> None:
    stub = StubRephraser("In persona, sir: all quiet.")
    composer = ResponseComposer(NEUTRAL, rephraser=stub)
    assert await composer.rephrase("All quiet.") == "In persona, sir: all quiet."
    assert stub.calls[0][1] == "All quiet."

    # failures and empty replies fall back to the deterministic text
    broken = ResponseComposer(NEUTRAL, rephraser=ExplodingRephraser())
    assert await broken.rephrase("All quiet.") == "All quiet."
    empty = ResponseComposer(NEUTRAL, rephraser=StubRephraser("  "))
    assert await empty.rephrase("All quiet.") == "All quiet."

    # no rephraser at all: identity
    assert await ResponseComposer(NEUTRAL).rephrase("text") == "text"
