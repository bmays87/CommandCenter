"""The default channel: structured log lines (always available)."""

import structlog

from prodeo.notify.interface import Notification

_log = structlog.get_logger("prodeo.notify")


class LogChannel:
    name = "log"

    async def send(self, notification: Notification) -> None:
        _log.info(
            "notification",
            title=notification.title,
            body=notification.body,
            priority=notification.priority,
            session_id=notification.session_id,
        )
