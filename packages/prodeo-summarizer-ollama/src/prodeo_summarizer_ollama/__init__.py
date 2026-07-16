"""Prodeo summarizer plugin: prose via a local Ollama model.

Implements the ``Summarizer`` Protocol (``prodeo.summary.interface``) with one
non-streaming call to Ollama's ``/api/chat``. Deliberately boring: the Summary
Service owns scheduling, digest building, timeouts, and containment — this
plugin only turns (instructions, content) into prose.
"""

from typing import Any

import httpx
from pydantic import BaseModel

from prodeo.plugins import PluginManifest

VERSION = "0.1.0"


class OllamaConfig(BaseModel):
    """Validated by the Plugin Host before the summarizer is constructed."""

    base_url: str = "http://localhost:11434"
    model: str = "llama3.2"
    #: HTTP timeout for one summarize call. The Summary Service applies its
    #: own overall timeout as well; this one bounds the socket.
    timeout_s: float = 120.0
    #: Passed through to Ollama's ``options`` (temperature, num_ctx, ...).
    options: dict[str, Any] = {}


class OllamaSummarizer:
    """One ``/api/chat`` round-trip per summary."""

    def __init__(self, config: OllamaConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._config = config
        self._transport = transport

    @property
    def name(self) -> str:
        return "ollama"

    async def summarize(self, instructions: str, content: str) -> str:
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": content},
            ],
            "stream": False,
        }
        if self._config.options:
            body["options"] = self._config.options
        async with httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=self._config.timeout_s,
            transport=self._transport,
        ) as client:
            response = await client.post("/api/chat", json=body)
            response.raise_for_status()
            data = response.json()
        text = str(data.get("message", {}).get("content", ""))
        return text.strip()


def create_summarizer(config: OllamaConfig) -> OllamaSummarizer:
    """Plugin factory: called by the host with the validated config."""
    return OllamaSummarizer(config)


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group): what this plugin is."""
    return PluginManifest(
        name="ollama",
        kind="summarizer",
        version=VERSION,
        config_model=OllamaConfig,
        factory=create_summarizer,
    )


__all__ = ["OllamaConfig", "OllamaSummarizer", "create_summarizer", "manifest"]
