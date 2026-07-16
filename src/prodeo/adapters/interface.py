"""The adapter contract (docs/architecture/adapter-specification.md).

Adapters teach Command Center to observe (and, capability permitting,
control) one kind of agent. The core contains zero agent-specific logic.
"""

from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from prodeo.errors import CapabilityNotSupportedError
from prodeo.mediation.model import Answer
from prodeo.sessions.model import SessionDescriptor

if TYPE_CHECKING:
    from prodeo.adapters.context import AdapterContext

#: Bumped when the adapter contract changes incompatibly. Adapters declare
#: the version they were built against; the manager refuses mismatches.
#: v2 (Phase 2): ``respond()`` joined the control surface.
ADAPTER_API_VERSION: Final = 2


class AdapterMetadata(BaseModel):
    name: str
    version: str
    adapter_api_version: int = ADAPTER_API_VERSION


class AdapterCapabilities(BaseModel):
    """Declared, not assumed - clients render controls from these flags."""

    observe: bool = True
    launch: bool = False
    terminate: bool = False
    respond_to_permissions: bool = False
    answer_questions: bool = False
    send_prompts: bool = False
    historical_sessions: bool = False


class SessionRef(BaseModel):
    """Identifies one session across the core/adapter boundary."""

    adapter: str
    native_id: str
    session_id: str  # Command-Center-assigned


class InteractionRef(BaseModel):
    """Identifies one interaction across the core/adapter boundary."""

    adapter: str
    session_native_id: str
    interaction_id: str  # Command-Center-assigned (ULID)
    native_id: str  # adapter-native (e.g. a tool_use_id)


class LaunchSpec(BaseModel):
    """How to start a new agent run (control adapters)."""

    project: str = ""  # working directory / project path
    prompt: str = ""
    model: str = ""
    permission_mode: str = ""
    #: Adapter-specific passthrough options (validated by the adapter).
    options: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class AgentAdapter(Protocol):
    """Implemented by adapter plugins. All methods async unless noted."""

    @property
    def metadata(self) -> AdapterMetadata: ...

    @property
    def capabilities(self) -> AdapterCapabilities: ...

    async def start(self, ctx: "AdapterContext") -> None: ...

    async def stop(self) -> None: ...

    # Observation (required)
    async def discover_sessions(self) -> list[SessionDescriptor]: ...

    async def watch(self, session: SessionRef) -> None:
        """Long-running task; report observations via ``ctx.report(...)``."""
        ...

    # Control (optional - guarded by capabilities)
    async def launch(self, spec: LaunchSpec) -> SessionRef: ...

    async def terminate(self, session: SessionRef) -> None: ...

    async def respond(self, interaction: InteractionRef, answer: Answer) -> None: ...

    async def send_prompt(self, session: SessionRef, prompt: str) -> None: ...


class ObserveOnlyAdapter:
    """Convenience base for adapters without control capabilities.

    Subclasses implement the observation surface; the control methods here
    raise :class:`CapabilityNotSupportedError`, which keeps capability
    declarations honest by default (the conformance kit verifies this).
    """

    async def launch(self, spec: LaunchSpec) -> SessionRef:
        raise CapabilityNotSupportedError("launch")

    async def terminate(self, session: SessionRef) -> None:
        raise CapabilityNotSupportedError("terminate")

    async def respond(self, interaction: InteractionRef, answer: Answer) -> None:
        raise CapabilityNotSupportedError("respond")

    async def send_prompt(self, session: SessionRef, prompt: str) -> None:
        raise CapabilityNotSupportedError("send_prompt")
