"""ntfy channel: push notifications to a topic on ntfy.sh or a self-hosted server.

Phone setup: install the ntfy app, subscribe to your topic, and route
``interaction.requested`` to this channel - tapping the notification opens the
dashboard inbox (``public_url`` must be set for the click-through).
"""

from typing import Any

import httpx

from prodeo.notify.interface import Notification

_PRIORITY = {"low": "2", "normal": "3", "high": "4"}


class NtfyChannel:
    name = "ntfy"

    def __init__(self, config: dict[str, Any], client: httpx.AsyncClient | None = None) -> None:
        self._server = str(config.get("server", "https://ntfy.sh")).rstrip("/")
        topic = config.get("topic")
        if not topic:
            raise ValueError("ntfy channel requires a 'topic' in notify_channels config")
        self._topic = str(topic)
        self._token = str(config.get("token", ""))
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def send(self, notification: Notification) -> None:
        headers = {
            "Title": notification.title.encode("ascii", "replace").decode(),
            "Priority": _PRIORITY[notification.priority],
        }
        if notification.url:
            headers["Click"] = notification.url
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = await self._client.post(
            f"{self._server}/{self._topic}",
            content=notification.body or notification.title,
            headers=headers,
        )
        resp.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
