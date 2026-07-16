"""Channel behavior: ntfy wire format (mocked HTTP), desktop degradation."""

import httpx
import pytest

from prodeo.notify.channels import DesktopChannel, NtfyChannel, channels_from_config
from prodeo.notify.interface import Notification


def _notification(**overrides: object) -> Notification:
    base: dict[str, object] = {
        "title": "Session failed: fixer",
        "body": "exit 1",
        "priority": "high",
        "url": "https://cc.example/#/inbox",
    }
    base.update(overrides)
    return Notification.model_validate(base)


@pytest.mark.asyncio
async def test_ntfy_posts_topic_with_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    channel = NtfyChannel(
        {"server": "https://ntfy.example/", "topic": "agents", "token": "tk-1"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await channel.send(_notification())

    (request,) = requests
    assert str(request.url) == "https://ntfy.example/agents"
    assert request.content == b"exit 1"
    assert request.headers["Title"] == "Session failed: fixer"
    assert request.headers["Priority"] == "4"
    assert request.headers["Click"] == "https://cc.example/#/inbox"
    assert request.headers["Authorization"] == "Bearer tk-1"
    await channel.close()


@pytest.mark.asyncio
async def test_ntfy_http_error_raises() -> None:
    channel = NtfyChannel(
        {"topic": "agents"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await channel.send(_notification())
    await channel.close()


def test_ntfy_requires_topic() -> None:
    with pytest.raises(ValueError, match="topic"):
        NtfyChannel({})


@pytest.mark.asyncio
async def test_desktop_missing_binary_raises_for_containment() -> None:
    channel = DesktopChannel({"binary": "definitely-not-a-real-binary"})
    with pytest.raises(OSError):
        await channel.send(_notification())


def test_channels_from_config_always_includes_log() -> None:
    channels = channels_from_config({"desktop": {}, "mystery": {"x": 1}})
    assert set(channels) == {"log", "desktop"}  # mystery skipped, log always on
