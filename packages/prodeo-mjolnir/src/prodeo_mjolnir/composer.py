"""The Response Composer: templates in, spoken sentences out.

Every response is deterministic template text (fast, predictable, offline).
The one exception is :meth:`rephrase` - an optional persona layer for
*non-time-critical* prose (the overnight briefing) via a ``summarizer``-kind
plugin, the same local-model path as the daily summary. Interaction
confirmations never pass through it: a permission answer must be fast,
predictable, and impossible to garble (voice-pipeline.md).
"""

import asyncio
import re

import structlog

from prodeo.summary.interface import Summarizer

_log = structlog.get_logger(__name__)

_REPHRASE_INSTRUCTIONS = (
    "Rewrite the following spoken status briefing in your persona's voice. "
    "Keep every fact, name, and number exactly as given; do not add, drop, or "
    "reorder information; keep it brief and speakable. Reply with the "
    "rewritten briefing only."
)


class ResponseComposer:
    """Renders template keys from a persona pack into speakable text."""

    def __init__(
        self,
        pack: dict[str, str],
        *,
        honorific: str = "",
        rephraser: Summarizer | None = None,
        rephrase_timeout_s: float = 10.0,
    ) -> None:
        self._pack = pack
        self._honorific = f", {honorific}" if honorific else ""
        self._rephraser = rephraser
        self._rephrase_timeout_s = rephrase_timeout_s

    def compose(self, key: str, **fields: str | int) -> str:
        """Render one template; unknown keys are a programming error."""
        template = self._pack[key]
        count = fields.get("count")
        if isinstance(count, int) and "plural" not in fields:
            fields["plural"] = "" if count == 1 else "s"
        text = template.format(honorific=self._honorific, **fields)
        # Interpolated titles carry their own punctuation ("Run it?."):
        # collapse doubled sentence enders so speech stays clean.
        return re.sub(r"([.?!])\.", r"\1", text)

    async def rephrase(self, text: str) -> str:
        """Persona-rephrase briefing prose; deterministic text on any failure."""
        if self._rephraser is None:
            return text
        try:
            async with asyncio.timeout(self._rephrase_timeout_s):
                rephrased = await self._rephraser.summarize(_REPHRASE_INSTRUCTIONS, text)
        except Exception as exc:
            _log.warning("composer.rephrase_failed", error=str(exc))
            return text
        return rephrased.strip() or text
