"""Desktop channel: OS notifications via ``notify-send``.

Only useful when the daemon runs on a host with a desktop session; in a
container or on a headless server the send fails and is reported as a
``notification.failed`` event (the Notifier contains it).
"""

import asyncio
from typing import Any

from prodeo.notify.interface import Notification

_URGENCY = {"low": "low", "normal": "normal", "high": "critical"}


class DesktopChannel:
    name = "desktop"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._binary = str(cfg.get("binary", "notify-send"))

    async def send(self, notification: Notification) -> None:
        process = await asyncio.create_subprocess_exec(
            self._binary,
            "--app-name=Prodeo",
            f"--urgency={_URGENCY[notification.priority]}",
            notification.title,
            notification.body or " ",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        code = await process.wait()
        if code != 0:
            raise RuntimeError(f"{self._binary} exited with {code}")
