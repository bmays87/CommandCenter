"""Domain errors for the voice client."""


class MjolnirError(Exception):
    """Base class for voice client errors."""


class EngineNotFoundError(MjolnirError):
    """A configured engine plugin is not installed (or is the wrong kind)."""

    def __init__(self, kind: str, name: str, installed: list[str]) -> None:
        available = ", ".join(sorted(installed)) or "none"
        super().__init__(
            f"no {kind} engine named {name!r} is installed (installed {kind} engines: "
            f"{available}); install the plugin package or change the setting"
        )


class AlreadyResolvedError(MjolnirError):
    """The interaction was answered elsewhere first (HTTP 409 from the server)."""


class ServerRequestError(MjolnirError):
    """The server rejected or failed a request."""
