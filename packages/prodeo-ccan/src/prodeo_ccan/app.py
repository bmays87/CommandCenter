"""The CCAN's HTTP surface: pairing and health.

Deliberately no authentication logic in the handlers: the TLS listener in
:mod:`prodeo_ccan.main` demands a client certificate and trusts exactly one
— the parent hub's (ADR-0025). Any request that reaches a handler has
already proven it comes from the parent, so handing back the enrollment
token here is handing it to the party that minted it.
"""

from fastapi import FastAPI
from pydantic import BaseModel

from prodeo.identity import Identity
from prodeo_ccan import __version__
from prodeo_ccan.config import CcanConfig


class PairRequest(BaseModel):
    """The hub introducing itself; informational."""

    hub_node: str = ""


class PairResponse(BaseModel):
    """Mirrors ``prodeo.machines.pairing._PairResponse``; keep in sync."""

    node: str
    name: str = ""
    version: str = ""
    certificate_pem: str = ""
    enroll_token: str


class CcanHealth(BaseModel):
    status: str
    node: str
    version: str


def create_app(config: CcanConfig, identity: Identity) -> FastAPI:
    app = FastAPI(title="Prodeo CCAN", version=__version__)

    @app.post("/ccan/v1/pair", response_model=PairResponse)
    async def pair(_body: PairRequest) -> PairResponse:
        """Answer the parent's Add Machine handshake (ADR-0025)."""
        return PairResponse(
            node=config.node_name,
            name=config.node_name,
            version=__version__,
            certificate_pem=identity.certificate_pem,
            enroll_token=config.enroll_token,
        )

    @app.get("/ccan/v1/health", response_model=CcanHealth)
    async def health() -> CcanHealth:
        return CcanHealth(status="ok", node=config.node_name, version=__version__)

    return app
