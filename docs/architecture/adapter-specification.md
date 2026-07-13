# Agent Adapter Specification

An adapter teaches Command Center how to observe and (where possible) control one
kind of agent. The core contains **zero** agent-specific logic; if you find yourself
writing `if adapter.name == "claude-code"` in core code, the design has failed.

## Design Tenets

1. **Observation before control.** Every agent can at minimum be *observed* (sessions
   discovered, output followed). Only some can be launched, stopped, or answered
   programmatically. The interface must not force capabilities an agent lacks.
2. **Capabilities are declared, not assumed.** Clients ask "can this session be
   paused?" and render accordingly. This is how one dashboard serves very different
   agents without lying to the user.
3. **Adapters are plugins.** They live in separate packages (`prodeo-adapter-*`),
   are versioned against a published `AdapterAPI` version, and are loaded via entry
   points (see plugin-system.md).

## Interface (abridged)

```python
class AgentAdapter(Protocol):
    """Implemented by adapter plugins. All methods async unless noted."""

    metadata: AdapterMetadata          # name, version, adapter_api_version
    capabilities: AdapterCapabilities  # see below

    async def start(self, ctx: AdapterContext) -> None: ...
    async def stop(self) -> None: ...

    # Observation (required)
    async def discover_sessions(self) -> list[SessionDescriptor]: ...
    async def watch(self, session: SessionRef) -> None:
        """Long-running task; report observations via ctx.report(...)."""

    # Control (optional — guarded by capabilities)
    async def launch(self, spec: LaunchSpec) -> SessionRef: ...
    async def terminate(self, session: SessionRef) -> None: ...
    async def respond(self, interaction: InteractionRef, answer: Answer) -> None: ...
    async def send_prompt(self, session: SessionRef, prompt: str) -> None: ...
```

```python
class AdapterCapabilities(BaseModel):
    observe: bool = True          # always true
    launch: bool = False
    terminate: bool = False
    respond_to_permissions: bool = False
    answer_questions: bool = False
    send_prompts: bool = False
    historical_sessions: bool = False
```

`AdapterContext` is the only door back into the core: `ctx.report(observation)`,
`ctx.logger`, `ctx.config`, `ctx.data_dir`. Adapters cannot publish arbitrary events;
they report typed **observations** which the Adapter Manager validates and translates
into domain events. This keeps a buggy adapter from corrupting the event stream.

## Conformance Suite

`prodeo.adapters.testing` ships a reusable pytest suite every adapter must pass:
lifecycle ordering, capability honesty (declaring `launch=True` but raising
`NotImplementedError` fails the suite), observation schema validity, and crash
containment (an adapter exception must never take down the manager).

## First Implementation: `prodeo-adapter-claude-code`

Strategy, in order of preference:

1. **Structured integration where offered** — Claude Code hooks and headless/SDK
   invocation for sessions Command Center launches itself (full capability set).
2. **Filesystem observation** for sessions the user started manually — watching the
   session JSONL transcripts under the Claude Code data directory (observe +
   historical only).

The JSONL format is **not a stable public contract**. The adapter therefore: pins
parsing logic behind a versioned internal parser, treats unknown record types as
opaque `agent.output_appended` events rather than failing, and carries a fixture
corpus of real transcripts in its test suite so upstream format drift is caught by CI
rather than by users. This fragility is the single biggest adapter risk and is
documented in ADR-0004.
