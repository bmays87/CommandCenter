# ADR-0005: Plugins are in-process Python packages discovered via entry points

- **Status**: Accepted
- **Date**: 2026-07-11

## Context
Everything replaceable is a plugin. Candidate mechanisms: entry points (in-process),
subprocess plugins with an IPC protocol, or WASM sandboxing.

## Decision
Entry points (`prodeo.plugins` group) with a versioned `plugin_api_version`
handshake, Pydantic-validated config, and contained failures. Plugins receive scoped
context objects, never the service container.

## Consequences
Trivial authoring and installation (`uv pip install prodeo-adapter-x`), full async
integration, no IPC layer to design. Cost: no security isolation — installing a
plugin is running code. We state this loudly in docs; it matches the trust model of
pip itself and of every comparable tool. A subprocess isolation mode can be added
behind the same manifest later if a plugin marketplace materializes; designing it
now is speculative.

## Alternatives Considered
**Subprocess plugins** (real isolation, but an IPC protocol + lifecycle manager is a
project in itself; revisit for the marketplace). **WASM** (Python-in-WASM for
arbitrary AI deps is not practical today).
