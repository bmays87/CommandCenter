"""Enrollment tokens: mint, claim-binding, persistence, corrupt files."""

from pathlib import Path

import pytest

from prodeo.machines.enrollments import Enrollments


@pytest.mark.asyncio
async def test_minted_token_claims_and_binds_to_first_node(tmp_path: Path) -> None:
    store = Enrollments(tmp_path / "enrollments.json")
    token = await store.mint(label="ccan-installer")

    assert await store.claim(token, node="worker-01") is True
    # Re-pairing the same machine keeps working ...
    assert await store.claim(token, node="worker-01") is True
    # ... but a lifted token cannot enroll a second machine.
    assert await store.claim(token, node="intruder") is False


@pytest.mark.asyncio
async def test_unknown_token_is_rejected(tmp_path: Path) -> None:
    store = Enrollments(tmp_path / "enrollments.json")
    await store.mint()
    assert await store.claim("not-a-minted-token", node="worker-01") is False


@pytest.mark.asyncio
async def test_tokens_survive_a_restart_and_stay_hashed(tmp_path: Path) -> None:
    path = tmp_path / "enrollments.json"
    token = await Enrollments(path).mint(label="one")

    # A second store over the same file (a rebooted hub) honors the claim...
    reopened = Enrollments(path)
    assert await reopened.claim(token, node="worker-01") is True
    # ...and the raw token never touches disk.
    assert token not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_corrupt_file_degrades_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "enrollments.json"
    path.write_text("{not json", encoding="utf-8")
    store = Enrollments(path)
    assert (await store.state()).tokens == {}
    # And it recovers: minting works over the corrupt file.
    token = await store.mint()
    assert await store.claim(token, node="n") is True
