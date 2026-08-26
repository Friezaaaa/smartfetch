"""Native MCP Streamable HTTP transport for the SmartFetch retrieval engine."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.routing import Route
from x402.schemas import PaymentRequirements, ResourceInfo

from .bazaar import FETCH_DESCRIPTION, fetch_mcp_discovery_extension
from .config import (
    HOST,
    MAX_REQUEST_BODY_BYTES,
)
from .payments import X402Settings, create_x402_resource_server


MCP_PATH = "/mcp"
MCP_TOOL = "fetch_webpage"
MCP_TRANSPORT = "streamable-http"
MCP_DEFAULT_MAX_CHARS = 20000
MCP_MIN_CHARS = 1000
MCP_MAX_CHARS = 50000
FetchHandler = Callable[[str, bool, int], Awaitable[dict]]


@dataclass
class SmartFetchMCP:
    fastmcp: FastMCP
    route: Route
    resource_server: Optional[object]
    accepts: list[PaymentRequirements]

    @asynccontextmanager
    async def lifespan(self, _application):
        async with self.fastmcp.session_manager.run():
            yield


def _initialize_payment(settings: X402Settings):
    initialization_failed = False
    resource_server = None
    accepts = []
    try:
        from x402.server import ResourceConfig

        resource_server = create_x402_resource_server(
            settings,
            register_bazaar=True,
        )
        resource_server.initialize()
        accepts = resource_server.build_payment_requirements(ResourceConfig(
            scheme="exact",
            payTo=settings.pay_to,
            price=settings.price,
            network=settings.network,
        ))
    except Exception:
        initialization_failed = True

    if initialization_failed:
        raise RuntimeError("x402 MCP initialization failed")
    return resource_server, accepts


def create_smartfetch_mcp(
    settings: X402Settings,
    fetch_handler: FetchHandler,
) -> SmartFetchMCP:
    """Create the single-tool native MCP server and eagerly secure it."""
    mcp = FastMCP(
        "SmartFetch",
        host=HOST,
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
    )
    resource_server = None
    accepts = []

    async def fetch_webpage(
        url: str,
        max_chars: Annotated[
            int,
            Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS),
        ] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict:
        return await fetch_handler(url, force_browser, max_chars)

    tool_handler = fetch_webpage
    if settings.enabled:
        resource_server, accepts = _initialize_payment(settings)
        from x402.mcp import create_payment_wrapper

        payment_wrapper = create_payment_wrapper(
            resource_server,
            accepts=accepts,
            resource=ResourceInfo(
                url=f"mcp://tool/{MCP_TOOL}",
                description=FETCH_DESCRIPTION,
                mimeType="application/json",
                serviceName="SmartFetch",
            ),
            extensions=fetch_mcp_discovery_extension(),
        )
        tool_handler = payment_wrapper(tool_handler)

    mcp.tool(
        name=MCP_TOOL,
        description=FETCH_DESCRIPTION,
        structured_output=False,
    )(tool_handler)

    http_app = mcp.streamable_http_app()
    route = next(
        candidate
        for candidate in http_app.routes
        if isinstance(candidate, Route) and candidate.path == MCP_PATH
    )
    return SmartFetchMCP(mcp, route, resource_server, accepts)
