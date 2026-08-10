"""JsonFileConfigStore: round-trip, absence, and corruption tolerance."""

from pathlib import Path

import pytest

from prodeo.extensions import JsonFileConfigStore


@pytest.mark.asyncio
async def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    store = JsonFileConfigStore(tmp_path / "never-written.json")
    assert await store.load() == {}
    assert await store.get("ollama") is None


@pytest.mark.asyncio
async def test_put_get_delete_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "extensions.json"
    store = JsonFileConfigStore(path)

    await store.put("ollama", {"model": "llama3.2"})
    assert await store.get("ollama") == {"model": "llama3.2"}

    # A second store over the same file sees the write - it is on disk, not
    # just in the cache.
    assert await JsonFileConfigStore(path).get("ollama") == {"model": "llama3.2"}

    await store.delete("ollama")
    assert await store.get("ollama") is None
    assert await JsonFileConfigStore(path).load() == {}


@pytest.mark.asyncio
async def test_put_creates_parent_directory(tmp_path: Path) -> None:
    store = JsonFileConfigStore(tmp_path / "nested" / "deeper" / "extensions.json")
    await store.put("ntfy", {"topic": "agents"})
    assert await store.get("ntfy") == {"topic": "agents"}


@pytest.mark.asyncio
async def test_corrupt_file_degrades_to_empty(tmp_path: Path) -> None:
    # A hand-edited overlay that no longer parses must not stop the server
    # booting: the environment layer alone is a working configuration.
    path = tmp_path / "extensions.json"
    path.write_text("{not json", encoding="utf-8")
    assert await JsonFileConfigStore(path).load() == {}


@pytest.mark.asyncio
async def test_non_object_entries_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "extensions.json"
    path.write_text('{"good": {"a": 1}, "bad": "not-a-mapping"}', encoding="utf-8")
    assert await JsonFileConfigStore(path).load() == {"good": {"a": 1}}


@pytest.mark.asyncio
async def test_delete_of_absent_name_is_a_noop(tmp_path: Path) -> None:
    store = JsonFileConfigStore(tmp_path / "extensions.json")
    await store.delete("never-there")  # must not raise
    assert await store.load() == {}


@pytest.mark.asyncio
async def test_legacy_flat_document_is_migrated_not_discarded(tmp_path: Path) -> None:
    # The first shipped format was a bare {name: config} map with no version.
    # Someone's saved settings must survive the upgrade.
    path = tmp_path / "extensions.json"
    path.write_text('{"ollama": {"model": "llama3.2"}}', encoding="utf-8")

    store = JsonFileConfigStore(path)
    assert await store.get("ollama") == {"model": "llama3.2"}
    state = await store.state()
    assert state.version == 1 and state.disabled == []


@pytest.mark.asyncio
async def test_enable_disable_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "extensions.json"
    store = JsonFileConfigStore(path)

    assert await store.disabled() == set()
    await store.set_enabled("parakeet", False)
    assert await store.disabled() == {"parakeet"}
    assert await JsonFileConfigStore(path).disabled() == {"parakeet"}  # persisted

    await store.set_enabled("parakeet", True)
    assert await store.disabled() == set()


@pytest.mark.asyncio
async def test_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "extensions.json"
    store = JsonFileConfigStore(path)

    assert (await store.settings()).models_dir == ""
    settings = await store.settings()
    settings.models_dir = str(tmp_path / "models")
    settings.autostart = ["mjolnir"]
    await store.put_settings(settings)

    reread = await JsonFileConfigStore(path).settings()
    assert reread.models_dir == str(tmp_path / "models")
    assert reread.autostart == ["mjolnir"]


@pytest.mark.asyncio
async def test_config_and_enablement_coexist_in_one_document(tmp_path: Path) -> None:
    path = tmp_path / "extensions.json"
    store = JsonFileConfigStore(path)
    await store.put("ollama", {"model": "llama3.2"})
    await store.set_enabled("parakeet", False)

    fresh = JsonFileConfigStore(path)
    assert await fresh.get("ollama") == {"model": "llama3.2"}
    assert await fresh.disabled() == {"parakeet"}


@pytest.mark.asyncio
async def test_returned_state_is_a_copy(tmp_path: Path) -> None:
    # Mutating what the store handed back must not silently change the store.
    store = JsonFileConfigStore(tmp_path / "extensions.json")
    await store.put("ollama", {"model": "a"})
    state = await store.state()
    state.config["ollama"]["model"] = "tampered"
    assert (await store.get("ollama")) == {"model": "a"}
