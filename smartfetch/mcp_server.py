"""Native MCP Streamable HTTP transport for the SmartFetch retrieval engine with x402 payment support."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
import json
import logging
from typing import Annotated, Any, Awaitable, Callable, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from starlette.routing import Route

from .activity import emit_activity
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
from .config import HOST, MAX_REQUEST_BODY_BYTES, SERVICE_NAME, SERVICE_VERSION
from .mcp_logging import configure_mcp_sdk_logging
from .payments import X402Settings, create_x402_resource_server
from .remote_browser import RemoteBrowserConfig


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

FetchHandler = Callable[[str, bool, int], Awaitable[dict[str, Any]]]

_LOGGER = logging.getLogger(__name__)


@dataclass
class SmartFetchMCP:
    fastmcp: FastMCP
    route: Route
    resource_server: Optional[Any]
    accepts: list[Any]

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
    except Exception as exc:
        _LOGGER.error("Failed to initialize x402 MCP payment server: %s", exc)
        initialization_failed = True

    if initialization_failed:
        raise RuntimeError("x402 MCP initialization failed")
    return resource_server, accepts


def create_smartfetch_mcp(
    settings: X402Settings,
    fetch_handler: FetchHandler,
    *,
    v111_service=None,
) -> SmartFetchMCP:
    """Build the production MCP server with optional x402 payment protection."""
    mcp = FastMCP(
        SERVICE_NAME,
        host=HOST,
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
    )
    mcp._mcp_server.version = SERVICE_VERSION
    configure_mcp_sdk_logging()

    resource_server = None
    accepts = []
    if settings.enabled:
        resource_server, accepts = _initialize_payment(settings)

    # Handlers for each of the 4 tools: fetch_webpage is the flagship tool,
    # and the remaining three tools delegate internally to fetch_webpage.
    async def fetch_webpage(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        return await fetch_handler(url.strip(), force_browser, max_chars)

    async def webpage_to_markdown(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        result = await fetch_webpage(url.strip(), max_chars=max_chars, force_browser=force_browser)
        md_text = result.get("markdown", "") or result.get("content", "")
        return {
            "success": True,
            "requested_url": url,
            "final_url": result.get("final_url", url),
            "status_code": result.get("status_code", 200),
            "render_method": result.get("render_method", "browser"),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "title": result.get("title", ""),
            "markdown": md_text,
        }

    async def extract_webpage_text(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        result = await fetch_webpage(url.strip(), max_chars=max_chars, force_browser=force_browser)
        text_content = result.get("content", "")
        return {
            "success": True,
            "requested_url": url,
            "final_url": result.get("final_url", url),
            "status_code": result.get("status_code", 200),
            "render_method": result.get("render_method", "browser"),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "title": result.get("title", ""),
            "content": text_content,
        }

    async def render_webpage(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        result = await fetch_webpage(url.strip(), max_chars=max_chars, force_browser=True)
        html_text = result.get("html", "") or result.get("content", "")
        return {
            "success": True,
            "requested_url": url,
            "final_url": result.get("final_url", url),
            "status_code": result.get("status_code", 200),
            "render_method": "browser",
            "elapsed_ms": result.get("elapsed_ms", 0),
            "title": result.get("title", ""),
            "html": html_text,
            "content": result.get("content", ""),
            "markdown": result.get("markdown", ""),
        }

    def protect(name: str, description: str, handler: Callable, extension: dict[str, Any] | None = None) -> Callable:
        if not settings.enabled:
            @wraps(handler)
            async def free_wrapper(**kwargs):
                try:
                    res = await handler(**kwargs)
                    text = json.dumps(res, ensure_ascii=False)
                    return CallToolResult(
                        content=[TextContent(type="text", text=text)],
                        structuredContent=res,
                        isError=False,
                    )
                except Exception as exc:
                    err_msg = str(exc)
                    return CallToolResult(
                        content=[TextContent(type="text", text=err_msg)],
                        structuredContent={"success": False, "error_code": "render_failed", "error": err_msg},
                        isError=True,
                    )
            return free_wrapper

        from x402.mcp import PaymentWrapperHooks, create_payment_wrapper
        from x402.schemas import ResourceInfo

        def payment_verified(_context):
            emit_activity(
                "payment_verified",
                transport="mcp",
                tool=name,
                stage="verification",
                outcome="verified",
                payment_present=True,
                payment_stage="verification",
                payment_network=settings.network,
                payment_asset="USDC",
                payment_amount=settings.price,
            )
            return True

        def payment_settled(_context):
            emit_activity(
                "payment_settled",
                transport="mcp",
                tool=name,
                stage="settlement",
                outcome="settled",
                payment_present=True,
                payment_stage="settlement",
                payment_network=settings.network,
                payment_asset="USDC",
                payment_amount=settings.price,
            )

        payment_wrapper = create_payment_wrapper(
            resource_server,
            accepts=accepts,
            resource=ResourceInfo(
                url=f"mcp://tool/{name}",
                description=description,
                mime_type="application/json",
            ),
            hooks=PaymentWrapperHooks(
                on_before_execution=payment_verified,
                on_after_settlement=payment_settled,
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

    remote_cfg = RemoteBrowserConfig.from_env()
    is_available = remote_cfg.is_configured

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
            meta={
                "availability": "available" if is_available else "unavailable",
                "payment_challenge_enabled": settings.enabled,
            },
            structured_output=False,
        )(tool_handler)

    # Cache static MCP tool listings for high-frequency census and directory bots
    _cached_tools: Optional[list[Any]] = None
    orig_list_tools = mcp.list_tools

    async def cached_list_tools():
        nonlocal _cached_tools
        if _cached_tools is None:
            _cached_tools = await orig_list_tools()
        return _cached_tools

    mcp.list_tools = cached_list_tools
    mcp._mcp_server.list_tools()(cached_list_tools)

    http_app = mcp.streamable_http_app()
    route = next(
        candidate
        for candidate in http_app.routes
        if isinstance(candidate, Route) and candidate.path == MCP_PATH
    )
    return SmartFetchMCP(mcp, route, resource_server, accepts)


def create_emergency_mcp() -> SmartFetchMCP:
    """Build fallback MCP server without payment requirements."""
    from .payments import load_x402_settings
    settings = load_x402_settings({"X402_ENABLED": "false"})

    async def _dummy_fetch(url: str, force_browser: bool, max_chars: int) -> dict[str, Any]:
        return {
            "success": False,
            "error_code": "retrieval_unavailable",
            "error": "Retrieval is temporarily unavailable",
        }

    return create_smartfetch_mcp(settings, _dummy_fetch)
