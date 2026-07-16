"""Ollama summarizer: request shape, response parsing, manifest contract."""

import json

import httpx
import pytest

from prodeo.plugins import PLUGIN_API_VERSION
from prodeo.summary.interface import Summarizer
from prodeo_summarizer_ollama import OllamaConfig, OllamaSummarizer, manifest


def _transport(reply: dict[str, object], captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=reply)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_summarize_sends_chat_request_and_returns_prose() -> None:
    captured: list[httpx.Request] = []
    summarizer = OllamaSummarizer(
        OllamaConfig(model="llama3.2", options={"temperature": 0.2}),
        transport=_transport(
            {"message": {"role": "assistant", "content": "  Two sessions finished.\n"}}, captured
        ),
    )

    prose = await summarizer.summarize("Summarize this.", "Sessions: 2 completed")

    assert prose == "Two sessions finished."
    request = captured[0]
    assert request.url.path == "/api/chat"
    body = json.loads(request.content)
    assert body["model"] == "llama3.2"
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.2}
    assert body["messages"][0] == {"role": "system", "content": "Summarize this."}
    assert body["messages"][1] == {"role": "user", "content": "Sessions: 2 completed"}


@pytest.mark.asyncio
async def test_http_errors_propagate_for_core_containment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model not found")

    summarizer = OllamaSummarizer(OllamaConfig(), transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await summarizer.summarize("x", "y")


def test_manifest_declares_the_summarizer_contract() -> None:
    m = manifest()
    assert m.kind == "summarizer"
    assert m.plugin_api_version == PLUGIN_API_VERSION
    assert m.config_model is OllamaConfig
    instance = m.factory(OllamaConfig())
    assert isinstance(instance, Summarizer)
    assert instance.name == "ollama"
