"""The notification channel contract (plugin kind ``notifier``)."""

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel


class Notification(BaseModel):
    """One human-facing notification, channel-agnostic."""

    title: str
    body: str = ""
    priority: Literal["low", "normal", "high"] = "normal"
    #: Click-through target (e.g. the dashboard inbox), when the channel
    #: supports links.
    url: str = ""
    event_id: str = ""
    session_id: str | None = None


@runtime_checkable
class NotificationChannel(Protocol):
    """Implemented by notification channels; ``send`` failures are contained
    by the Notifier and become ``notification.failed`` events."""

    @property
    def name(self) -> str: ...

    async def send(self, notification: Notification) -> None: ...
