"""The Notifier service: event stream in, channel sends out.

Subscribes to the bus (best-effort - a flood of events must never block the
core), matches each event against user routing rules (event type pattern ->
channel names), formats a human-facing :class:`Notification`, and sends it to
each routed channel. Sends are contained: a failing channel becomes a
``notification.failed`` event, never an exception in the core.

``notification.*`` events themselves are never routed (loop guard).
"""

import asyncio
import contextlib
from typing import Any

import structlog

from prodeo.bus.interface import BackpressurePolicy, EventBus, Subscription, matches
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.notify.interface import Notification, NotificationChannel

_log = structlog.get_logger(__name__)

_SOURCE = "notifier"


def _format(event: Event, public_url: str) -> Notification:
    """Shape one event into a human-facing notification."""
    payload = event.payload
    if event.type == ev.INTERACTION_REQUESTED:
        interaction: dict[str, Any] = payload.get("interaction", {})
        kind = str(interaction.get("kind", "interaction"))
        return Notification(
            title=f"[{kind}] {interaction.get('title', 'An agent needs you')}",
            body=str(interaction.get("body", ""))[:500],
            priority="high",
            url=f"{public_url}/#/inbox" if public_url else "",
            event_id=event.id,
            session_id=event.session_id,
        )
    if event.type in (ev.SESSION_COMPLETED, ev.SESSION_FAILED, ev.SESSION_STOPPED):
        outcome = event.type.split(".")[1]
        subject = str(payload.get("title") or payload.get("project") or event.session_id or "")
        return Notification(
            title=f"Session {outcome}: {subject}".strip(),
            body=str(payload.get("reason", "")),
            priority="high" if event.type == ev.SESSION_FAILED else "normal",
            url=f"{public_url}/#/session/{event.session_id}" if public_url else "",
            event_id=event.id,
            session_id=event.session_id,
        )
    return Notification(
        title=event.type,
        body=str(payload)[:500],
        event_id=event.id,
        session_id=event.session_id,
    )


class Notifier:
    """Routes events to notification channels according to config rules."""

    def __init__(
        self,
        bus: EventBus,
        channels: dict[str, NotificationChannel],
        rules: dict[str, list[str]],
        *,
        node: str = "local",
        public_url: str = "",
    ) -> None:
        self._bus = bus
        self._channels = channels
        self._rules = rules
        self._node = node
        self._public_url = public_url
        self._task: asyncio.Task[None] | None = None
        self._sub: Subscription | None = None

    async def start(self) -> None:
        self._sub = self._bus.subscribe("*", name="notifier", policy=BackpressurePolicy.DROP_OLDEST)
        self._task = asyncio.create_task(self._run(), name="notifier")
        _log.info("notifier.started", channels=sorted(self._channels), rules=len(self._rules))

    async def stop(self) -> None:
        if self._sub is not None:
            await self._sub.close()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        assert self._sub is not None
        async for event in self._sub:
            try:
                await self._route(event)
            except Exception:  # a formatter bug must not kill the loop
                _log.exception("notifier.route_failed", event_id=event.id)

    async def _route(self, event: Event) -> None:
        if event.type.startswith("notification."):
            return  # loop guard
        targets: list[str] = []
        for pattern, channel_names in self._rules.items():
            if matches(pattern, event.type):
                targets.extend(n for n in channel_names if n not in targets)
        if not targets:
            return
        notification = _format(event, self._public_url)
        for name in targets:
            channel = self._channels.get(name)
            if channel is None:
                await self._failed(name, notification, "unknown channel")
                continue
            try:
                await channel.send(notification)
            except Exception as exc:
                _log.warning("notifier.send_failed", channel=name, error=str(exc))
                await self._failed(name, notification, str(exc))
                continue
            await self._bus.publish(
                new_event(
                    ev.NOTIFICATION_SENT,
                    node=self._node,
                    source=_SOURCE,
                    session_id=notification.session_id,
                    payload={
                        "channel": name,
                        "event_id": notification.event_id,
                        "title": notification.title,
                    },
                )
            )

    async def _failed(self, channel: str, notification: Notification, error: str) -> None:
        await self._bus.publish(
            new_event(
                ev.NOTIFICATION_FAILED,
                node=self._node,
                source=_SOURCE,
                session_id=notification.session_id,
                payload={
                    "channel": channel,
                    "event_id": notification.event_id,
                    "title": notification.title,
                    "error": error,
                },
            )
        )
