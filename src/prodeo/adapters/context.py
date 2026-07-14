"""AdapterContext: the only door from an adapter back into the core."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from prodeo.adapters.observations import Observation

ReportFn = Callable[[Observation], Awaitable[None]]


class AdapterContext:
    """Scoped services handed to an adapter at ``start()``.

    Deliberately narrow: report typed observations, log, read validated
    config, and use a private data directory. Nothing else.
    """

    def __init__(
        self,
        adapter_name: str,
        report: ReportFn,
        config: dict[str, Any],
        data_dir: Path,
    ) -> None:
        self._report = report
        self.config = config
        self.data_dir = data_dir
        self.logger = structlog.get_logger("prodeo.adapter").bind(adapter=adapter_name)

    async def report(self, observation: Observation) -> None:
        """Hand one observation to the Adapter Manager."""
        await self._report(observation)
