"""The summarizer contract (plugin kind ``summarizer``).

A summarizer turns a structured activity digest into short prose. The
reference implementation is ``prodeo-summarizer-ollama`` (local models via
Ollama); the core works without one — the digest is still published, just
without prose.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Summarizer(Protocol):
    """Implemented by summarizer plugins; failures are contained by the
    Summary Service and reported in the ``summary.generated`` payload."""

    @property
    def name(self) -> str: ...

    async def summarize(self, instructions: str, content: str) -> str:
        """Return prose for ``content`` following ``instructions``."""
        ...
