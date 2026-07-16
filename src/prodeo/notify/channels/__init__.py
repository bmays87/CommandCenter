"""Built-in notification channels and their config-driven construction."""

from typing import Any

import structlog

from prodeo.notify.channels.desktop import DesktopChannel
from prodeo.notify.channels.log import LogChannel
from prodeo.notify.channels.ntfy import NtfyChannel
from prodeo.notify.interface import NotificationChannel

_log = structlog.get_logger(__name__)

__all__ = ["DesktopChannel", "LogChannel", "NtfyChannel", "channels_from_config"]


def channels_from_config(config: dict[str, dict[str, Any]]) -> dict[str, NotificationChannel]:
    """Build the channel map from ``Settings.notify_channels``.

    The log channel is always present. Unknown channel names are reported and
    skipped (third-party channel plugins arrive with the Phase 3 plugin host).
    """
    channels: dict[str, NotificationChannel] = {"log": LogChannel()}
    for name, cfg in config.items():
        if name == "ntfy":
            channels[name] = NtfyChannel(cfg)
        elif name == "desktop":
            channels[name] = DesktopChannel(cfg)
        elif name == "log":
            continue
        else:
            _log.warning("notify.unknown_channel", channel=name)
    return channels
