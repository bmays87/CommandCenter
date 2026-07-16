# ADR-0008: Claude Code control via the Agent SDK; transcripts stay the observation source

- **Status**: Accepted
- **Date**: 2026-07-16

## Context
Phase 2 gives the claude-code adapter launch/terminate/respond. Two candidate
mechanisms: drive the `claude` CLI directly (`-p --input-format stream-json`,
`--permission-prompt-tool`) and own the wire protocol, or depend on the Python
`claude-agent-sdk` (`ClaudeSDKClient`), whose `can_use_tool` callback may await
an external decision indefinitely — exactly the seam mediation needs. Launched
sessions also write the same JSONL transcripts the adapter already watches, so
there are two possible observation paths for one session.

## Decision
1. Depend on `claude-agent-sdk` (adapter package only; the core stays
   dependency-free). All SDK contact is isolated in `launcher.py` behind an
   injectable `client_factory`, so tests fake the SDK and a CLI fallback could
   be swapped in without touching the adapter.
2. **The transcript watcher remains the only observation source.** The SDK
   message stream is control-only: it yields the session id, hosts the
   `can_use_tool` permission bridge (an `asyncio.Future[Answer]` resolved by
   `respond()`), and detects failures the transcript cannot show. This avoids
   double-reporting and keeps launched and manual sessions on one code path.
3. Control applies only to sessions this server launched (tracked in an
   owned-session set); capabilities degrade to observe-only when the SDK is
   missing or `control_enabled` is false, keeping capability declarations
   honest (ADR-0004).

## Consequences
Small adapter surface over a supported integration instead of a hand-rolled
protocol; permission mediation needs no timeout gymnastics (the SDK pauses
until we answer). Costs: coupling to SDK release cadence (mitigated by the
factory seam and conformance suite), and a launch→transcript-creation gap that
`watch()` bridges by polling briefly for the file before declaring a session
stopped.

## Alternatives Considered
**Raw CLI stream-json** — no dependency, but we own protocol drift and the
`--permission-prompt-tool` wire format is under-documented. **SDK stream as
the observation source for launched sessions** — richer live data but forks
the event pipeline in two and invites double-reporting.
