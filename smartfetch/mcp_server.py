"""Unwrapped, native MCP identities for the emergency fail-closed service."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from starlette.routing import Route

from .config import HOST, MAX_REQUEST_BODY_BYTES
from .mcp_logging import configure_mcp_sdk_logging


MCP_PATH = "/mcp"
MCP_TOOL = "fetch_webpage"
MCP_TOOLS = (
    MCP_TOOL,
    "webpage_to_markdown",
    "extract_webpage_text",
    "render_webpage",
)
V111_MCP_TOOLS = ("search_and_extract", "extract_structured_data")
MCP_TRANSPORT = "streamable-http"
MCP_DEFAULT_MAX_CHARS = 20000
MCP_MIN_CHARS = 1000
MCP_MAX_CHARS = 50000


@dataclass
class SmartFetchMCP:
    fastmcp: FastMCP
    route: Route
    resource_server: None
    accepts: list

    @asynccontextmanager
    async def lifespan(self, _application):
        async with self.fastmcp.session_manager.run():
            yield


def _unavailable(browser: bool) -> CallToolResult:
    code = "browser_unavailable" if browser else "retrieval_unavailable"
    message = (
        "Browser rendering is temporarily unavailable"
        if browser else "Retrieval is temporarily unavailable"
    )
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=message)],
        structuredContent={"success": False, "error_code": code, "error": message},
    )


def create_emergency_mcp() -> SmartFetchMCP:
    """Build exactly four free, unavailable tool identities; no paid machinery."""
    mcp = FastMCP(
        "SmartFetch",
        host=HOST,
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
    )
    configure_mcp_sdk_logging()

    async def fetch_webpage(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> CallToolResult:
        return _unavailable(force_browser)

    async def webpage_to_markdown(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> CallToolResult:
        return _unavailable(force_browser)

    async def extract_webpage_text(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> CallToolResult:
        return _unavailable(force_browser)

    async def render_webpage(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
    ) -> CallToolResult:
        return _unavailable(True)

    for name, description, handler in (
        (MCP_TOOL, "Temporarily unavailable in emergency fail-closed mode; webpage retrieval cannot execute.", fetch_webpage),
        ("webpage_to_markdown", "Temporarily unavailable in emergency fail-closed mode; Markdown retrieval cannot execute.", webpage_to_markdown),
        ("extract_webpage_text", "Temporarily unavailable in emergency fail-closed mode; text extraction cannot execute.", extract_webpage_text),
        ("render_webpage", "Browser rendering is temporarily unavailable in emergency fail-closed mode.", render_webpage),
    ):
        mcp.tool(
            name=name,
            description=description,
            meta={"availability": "unavailable", "payment_challenge_enabled": False},
            structured_output=False,
        )(handler)

    http_app = mcp.streamable_http_app()
    route = next(
        candidate
        for candidate in http_app.routes
        if isinstance(candidate, Route) and candidate.path == MCP_PATH
    )
    return SmartFetchMCP(mcp, route, None, [])


def create_smartfetch_mcp(settings, fetch_handler, *, v111_service=None) -> SmartFetchMCP:
    """Retain the old caller signature, never its paid execution behavior."""
    return create_emergency_mcp()
