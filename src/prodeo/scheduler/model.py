"""Schedule domain models shared by the scheduler service and the API."""

from datetime import datetime

from pydantic import BaseModel

from prodeo.adapters.interface import LaunchSpec


class Schedule(BaseModel):
    """One cron-style agent launch definition.

    Schedules are event-sourced: ``schedule.created`` carries the full model,
    ``schedule.deleted`` removes it. ``last_triggered_at`` folds from
    ``schedule.triggered``; ``next_run_at`` is recomputed at runtime and is
    informational for API readers.
    """

    id: str
    name: str
    cron: str
    adapter: str
    spec: LaunchSpec
    created_at: datetime
    last_triggered_at: datetime | None = None
    next_run_at: datetime | None = None
