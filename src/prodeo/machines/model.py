"""Machine domain model shared by the registry and the API."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class Machine(BaseModel):
    """One agent machine known to the hub.

    ``node`` is the machine's identity (its ``PRODEO_NODE_NAME``, stamped on
    every event it originates); ``name`` is the display name shown on its
    dashboard tab and is the only renameable field. Renaming never touches
    ``node`` — history stays attributed.
    """

    id: str
    node: str
    name: str
    #: FQDN or IP address its CCAN answers on. None = the hub's own
    #: (colocated) machine, which needs no address and cannot be removed.
    address: str | None = None
    added_at: datetime = Field(default_factory=_now)
