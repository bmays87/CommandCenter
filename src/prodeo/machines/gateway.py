"""Forwarding hub calls to the CCAN that owns a machine (ADR-0026).

Every call rides the pinned channel: the hub presents its client certificate
(the only one a CCAN answers) and verifies the node against the certificate
recorded at pairing — both directions of ADR-0025's trust model, now with
the trust-on-first-use window closed.
"""

from typing import Any, Protocol

import httpx
import structlog

from prodeo.errors import RemoteNodeError
from prodeo.identity import IdentityProvider
from prodeo.machines.pairing import node_url, pinned_client
from prodeo.machines.registry import MachineRegistry

_log = structlog.get_logger(__name__)


class NodeChannel(Protocol):
    """What the API and node sync need from forwarding; faked in tests."""

    async def forward(
        self, node: str, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        """Call the CCAN owning ``node``; return its parsed JSON body.

        Raises :class:`RemoteNodeError` carrying the CCAN's status and
        detail when it refuses, or 502 when no machine/route exists or the
        node is unreachable.
        """
        ...


class NodeGateway:
    """The real channel: machine lookup, pinned TLS, error translation."""

    def __init__(
        self,
        identity: IdentityProvider,
        machines: MachineRegistry,
        *,
        timeout_s: float = 35.0,
    ) -> None:
        self._identity = identity
        self._machines = machines
        self._timeout_s = timeout_s

    async def forward(
        self, node: str, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        machine = self._machines.get_by_node(node)
        if machine is None or machine.address is None:
            raise RemoteNodeError(f"no paired machine for node {node!r}")
        identity = await self._identity.get()
        try:
            async with pinned_client(
                identity, machine.certificate, timeout_s=self._timeout_s
            ) as client:
                resp = await client.request(method, node_url(machine.address) + path, json=payload)
        except httpx.HTTPError as exc:
            raise RemoteNodeError(
                f"machine {machine.name!r} ({machine.address}) is unreachable: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise RemoteNodeError(_detail(resp), status_code=resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    return str(detail) if detail else f"the CCAN answered HTTP {resp.status_code}"
