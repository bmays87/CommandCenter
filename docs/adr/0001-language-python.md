# ADR-0001: Core language is Python 3.12+

- **Status**: Accepted
- **Date**: 2026-07-11

## Context
The brief allows Python or C#. The system is I/O-bound (subprocess supervision, file
watching, websockets), integrates heavily with the local-AI ecosystem (Ollama
clients, Piper, OpenWakeWord, faster-whisper, NeMo), and its most valuable external
contribution is adapters.

## Decision
Python 3.12+ with asyncio, typed strictly (mypy --strict + Pydantic at boundaries).

## Consequences
First-class access to every preferred local-AI library; the contributor pool that
writes AI tooling overwhelmingly writes Python; adapter authors can crib from agent
vendors' own Python SDKs. We accept the costs: weaker in-process parallelism (GIL) —
mitigated because the core is I/O-bound and heavy inference lives in the voice
client/plugins — and packaging discipline handled by uv.

## Alternatives Considered
**C# / .NET 8**: excellent runtime and concurrency story, but nearly every
integration target (wake word, STT, Ollama tooling, agent SDKs) is Python-first;
bindings would be perpetual friction, and the adapter-contributor pool shrinks.
A polyglot split (C# core + Python plugins) was rejected as complexity without a
driving requirement.
