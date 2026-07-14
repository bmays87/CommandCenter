"""Single-token authentication (v1).

Every route passes through this dependency so multi-user auth can replace it
later without touching handlers. When no token is configured the API is open -
intended for localhost development only, and loudly logged at startup.
"""

from collections.abc import Callable
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, WebSocket, status

_log = structlog.get_logger(__name__)


def _presented(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get("token")


def make_auth_dependency(token: str | None) -> Callable[..., None]:
    """Build the REST auth dependency for a configured token (None = open)."""
    if token is None:
        _log.warning("api.auth_disabled", hint="set PRODEO_API_TOKEN to require auth")

        def open_access() -> None:
            return None

        return open_access

    def require_token(presented: Annotated[str | None, Depends(_presented)]) -> None:
        if presented != token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token


def websocket_authorized(ws: WebSocket, token: str | None) -> bool:
    """WebSocket auth check (browsers cannot set headers, so ?token= is fine)."""
    if token is None:
        return True
    header = ws.headers.get("authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else None
    return token == (presented or ws.query_params.get("token"))
