"""Native MCP Streamable HTTP transport for the SmartFetch retrieval engine."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.routing import Route
from x402.schemas import PaymentRequirements, ResourceInfo

from .bazaar import (
    FETCH_DESCRIPTION,
    FETCH_INPUT_EXAMPLE,
    FETCH_INPUT_SCHEMA,
    FETCH_OUTPUT_EXAMPLE,
    FETCH_OUTPUT_SCHEMA,
    MARKDOWN_DESCRIPTION,
    MARKDOWN_OUTPUT_EXAMPLE,
    MARKDOWN_OUTPUT_SCHEMA,
    RENDER_DESCRIPTION,
    RENDER_INPUT_EXAMPLE,
    RENDER_INPUT_SCHEMA,
    RENDER_OUTPUT_EXAMPLE,
    TEXT_DESCRIPTION,
    TEXT_OUTPUT_EXAMPLE,
    TEXT_OUTPUT_SCHEMA,
    mcp_discovery_extension,
)
from .config import (
    HOST,
    MAX_REQUEST_BODY_BYTES,
)
from .payments import X402Settings, create_x402_resource_server


MCP_PATH = "/mcp"
MCP_TOOL = "fetch_webpage"
MCP_TOOLS = (
    MCP_TOOL,
    "webpage_to_markdown",
    "extract_webpage_text",
    "render_webpage",
)
MCP_TRANSPORT = "streamable-http"
MCP_DEFAULT_MAX_CHARS = 20000
MCP_MIN_CHARS = 1000
MCP_MAX_CHARS = 50000
FetchHandler = Callable[[str, bool, int], Awaitable[dict]]
COMMON_PROJECTION_FIELDS = (
    "success",
    "requested_url",
    "final_url",
    "status_code",
    "render_method",
    "elapsed_ms",
    "fallback_reason",
    "title",
    "truncated",
    "max_chars",
    "request_id",
    "service_version",
)


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


def _project_result(result: dict, primary_field: str) -> dict:
    fields = (*COMMON_PROJECTION_FIELDS, primary_field)
    return {key: result[key] for key in fields if key in result}


def create_smartfetch_mcp(
    settings: X402Settings,
    fetch_handler: FetchHandler,
) -> SmartFetchMCP:
    """Create the native MCP server and eagerly secure each paid tool."""
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

    async def webpage_to_markdown(
        url: str,
        max_chars: Annotated[
            int,
            Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS),
        ] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict:
        result = await fetch_handler(url, force_browser, max_chars)
        return _project_result(result, "markdown")

    async def extract_webpage_text(
        url: str,
        max_chars: Annotated[
            int,
            Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS),
        ] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict:
        result = await fetch_handler(url, force_browser, max_chars)
        return _project_result(result, "content")

    async def render_webpage(
        url: str,
        max_chars: Annotated[
            int,
            Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS),
        ] = MCP_DEFAULT_MAX_CHARS,
    ) -> dict:
        return await fetch_handler(url, True, max_chars)

    if settings.enabled:
        resource_server, accepts = _initialize_payment(settings)
        from x402.mcp import create_payment_wrapper

    def protect(name, description, handler, extension):
        if not settings.enabled:
            return handler
        payment_wrapper = create_payment_wrapper(
            resource_server,
            accepts=accepts,
            resource=ResourceInfo(
                url=f"mcp://tool/{name}",
                description=description,
                mimeType="application/json",
                serviceName="SmartFetch",
            ),
            extensions=extension,
        )
        return payment_wrapper(handler)

    tools = (
        (
            MCP_TOOL,
            FETCH_DESCRIPTION,
            fetch_webpage,
            FETCH_INPUT_SCHEMA,
            FETCH_INPUT_EXAMPLE,
            FETCH_OUTPUT_SCHEMA,
            FETCH_OUTPUT_EXAMPLE,
        ),
        (
            "webpage_to_markdown",
            MARKDOWN_DESCRIPTION,
            webpage_to_markdown,
            FETCH_INPUT_SCHEMA,
            FETCH_INPUT_EXAMPLE,
            MARKDOWN_OUTPUT_SCHEMA,
            MARKDOWN_OUTPUT_EXAMPLE,
        ),
        (
            "extract_webpage_text",
            TEXT_DESCRIPTION,
            extract_webpage_text,
            FETCH_INPUT_SCHEMA,
            FETCH_INPUT_EXAMPLE,
            TEXT_OUTPUT_SCHEMA,
            TEXT_OUTPUT_EXAMPLE,
        ),
        (
            "render_webpage",
            RENDER_DESCRIPTION,
            render_webpage,
            RENDER_INPUT_SCHEMA,
            RENDER_INPUT_EXAMPLE,
            FETCH_OUTPUT_SCHEMA,
            RENDER_OUTPUT_EXAMPLE,
        ),
    )
    for (
        name,
        description,
        handler,
        input_schema,
        input_example,
        output_schema,
        output_example,
    ) in tools:
        extension = mcp_discovery_extension(
            tool_name=name,
            description=description,
            input_schema=input_schema,
            input_example=input_example,
            output_schema=output_schema,
            output_example=output_example,
            transport=MCP_TRANSPORT,
        )
        tool_handler = protect(name, description, handler, extension)
        mcp.tool(
            name=name,
            description=description,
            structured_output=False,
        )(tool_handler)

    http_app = mcp.streamable_http_app()
    route = next(
        candidate
        for candidate in http_app.routes
        if isinstance(candidate, Route) and candidate.path == MCP_PATH
    )
    return SmartFetchMCP(mcp, route, resource_server, accepts)
