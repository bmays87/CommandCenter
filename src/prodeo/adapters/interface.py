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
#: v3: ``set_model()`` joined the control surface.
#: v4: ``set_permission_mode()`` joined the control surface.
#: v5: ``interrupt()`` and ``context_usage()`` joined the control surface.
ADAPTER_API_VERSION: Final = 5


class ModelInfo(BaseModel):
    """One model an adapter can launch/switch to.

    ``id`` is adapter-native (an alias or a full model id); free-form ids
    remain legal at every API that accepts a model. An empty catalog means
    "the adapter takes free-form ids only".
    """

    id: str
    label: str = ""
    default: bool = False


class AdapterMetadata(BaseModel):
    name: str
    version: str
    adapter_api_version: int = ADAPTER_API_VERSION
    #: Declared model catalog (like capabilities: declarations, not queries).
    models: list[ModelInfo] = Field(default_factory=list)


class AdapterCapabilities(BaseModel):
    """Declared, not assumed - clients render controls from these flags."""

    observe: bool = True
    launch: bool = False
    terminate: bool = False
    respond_to_permissions: bool = False
    answer_questions: bool = False
    send_prompts: bool = False
    set_model: bool = False
    set_permission_mode: bool = False
    #: Can stop the current turn without ending the session.
    interrupt: bool = False
    #: Can report context-window usage for a live session.
    report_context: bool = False
    historical_sessions: bool = False


class AdapterInfo(BaseModel):
    """One loaded adapter as exposed to clients (``GET /api/adapters``).

    Lives here (not in the API layer) so clients such as Mjolnir can import
    the model — mirroring how ``ClientPresence`` lives in ``prodeo.presence``.
    """

    name: str
    version: str
    capabilities: AdapterCapabilities
    models: list[ModelInfo] = Field(default_factory=list)


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

    async def set_model(self, session: SessionRef, model: str) -> None:
        """Switch the session's model (empty ``model`` = the agent's default)."""
        ...

    async def set_permission_mode(self, session: SessionRef, mode: str) -> None:
        """Switch how the session handles permissions (adapter-native mode)."""
        ...

    async def interrupt(self, session: SessionRef) -> None:
        """Stop the current turn but keep the session alive for more input."""
        ...

    async def context_usage(self, session: SessionRef) -> dict[str, Any]:
        """Context-window usage for a live session (adapter-native shape)."""
        ...


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

    async def set_model(self, session: SessionRef, model: str) -> None:
        raise CapabilityNotSupportedError("set_model")

    async def set_permission_mode(self, session: SessionRef, mode: str) -> None:
        raise CapabilityNotSupportedError("set_permission_mode")

    async def interrupt(self, session: SessionRef) -> None:
        raise CapabilityNotSupportedError("interrupt")

    async def context_usage(self, session: SessionRef) -> dict[str, Any]:
        raise CapabilityNotSupportedError("context_usage")
