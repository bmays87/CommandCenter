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
