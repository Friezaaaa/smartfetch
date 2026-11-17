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
    INSPECT_DESCRIPTION,
    INSPECT_INPUT_EXAMPLE,
    INSPECT_INPUT_SCHEMA,
    INSPECT_OUTPUT_EXAMPLE,
    INSPECT_OUTPUT_SCHEMA,
    MARKDOWN_DESCRIPTION,
    MARKDOWN_OUTPUT_EXAMPLE,
    MARKDOWN_OUTPUT_SCHEMA,
    RENDER_DESCRIPTION,
    RENDER_INPUT_EXAMPLE,
    RENDER_INPUT_SCHEMA,
    RENDER_OUTPUT_EXAMPLE,
    RENDER_OUTPUT_SCHEMA,
    SCREENSHOT_DESCRIPTION,
    SCREENSHOT_INPUT_EXAMPLE,
    SCREENSHOT_INPUT_SCHEMA,
    SCREENSHOT_OUTPUT_EXAMPLE,
    SCREENSHOT_OUTPUT_SCHEMA,
    TEXT_DESCRIPTION,
    TEXT_OUTPUT_EXAMPLE,
    TEXT_OUTPUT_SCHEMA,
    inspect_mcp_discovery_extension,
    mcp_discovery_extension,
    screenshot_mcp_discovery_extension,
)
from .browser_fetch import browser_screenshot
from .config import HOST, MAX_REQUEST_BODY_BYTES, SERVICE_NAME, SERVICE_VERSION
from .inspect import inspect_webpage_url
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
    "screenshot_webpage",
    "inspect_webpage",
)
V111_MCP_TOOLS = ("search_and_extract", "extract_structured_data")
MCP_TRANSPORT = "streamable-http"
MCP_DEFAULT_MAX_CHARS = 20000
MCP_MIN_CHARS = 1000
MCP_MAX_CHARS = 50000

FetchHandler = Callable[..., Awaitable[dict[str, Any]]]
ScreenshotHandler = Callable[..., Awaitable[dict[str, Any]]]
InspectHandler = Callable[..., Awaitable[dict[str, Any]]]

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
    screenshot_accepts = []
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
        screenshot_accepts = resource_server.build_payment_requirements(ResourceConfig(
            scheme="exact",
            payTo=settings.pay_to,
            price="$0.01",
            network=settings.network,
        ))
    except Exception as exc:
        _LOGGER.error("Failed to initialize x402 MCP payment server: %s", exc)
        initialization_failed = True

    if initialization_failed:
        raise RuntimeError("x402 MCP initialization failed")
    return resource_server, accepts, screenshot_accepts


def create_smartfetch_mcp(
    settings: X402Settings,
    fetch_handler: FetchHandler,
    *,
    screenshot_handler: ScreenshotHandler | None = None,
    inspect_handler: InspectHandler | None = None,
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
    screenshot_accepts = []
    if settings.enabled:
        resource_server, accepts, screenshot_accepts = _initialize_payment(settings)

    # Handlers for each of the 4 tools: fetch_webpage is the flagship tool,
    # and the remaining three tools delegate internally to fetch_handler.
    async def fetch_webpage(
        url: Annotated[str, Field(description="Public HTTP or HTTPS URL of a webpage or PDF to retrieve.")],
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS, description="Maximum characters returned for content and Markdown.")] = MCP_DEFAULT_MAX_CHARS,
        force_browser: Annotated[bool, Field(description="Start with browser rendering instead of fast HTTP retrieval.")] = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        return await fetch_handler(url.strip(), force_browser, max_chars)

    async def webpage_to_markdown(
        url: Annotated[str, Field(description="Public HTTP or HTTPS URL of a webpage or PDF to retrieve as Markdown.")],
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS, description="Maximum characters returned for Markdown.")] = MCP_DEFAULT_MAX_CHARS,
        force_browser: Annotated[bool, Field(description="Start with browser rendering instead of fast HTTP retrieval.")] = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        result = await fetch_handler(url.strip(), force_browser, max_chars)
        md_text = result.get("markdown", "") or result.get("content", "")
        payload: dict[str, Any] = {
            "success": bool(result.get("success", True)),
            "requested_url": result.get("requested_url", url.strip()),
            "final_url": result.get("final_url", url.strip()),
            "status_code": result.get("status_code", 200),
            "render_method": result.get("render_method", "browser" if force_browser else "http"),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "title": result.get("title", ""),
            "markdown": md_text,
            "truncated": bool(result.get("truncated", False)),
            "max_chars": result.get("max_chars", max_chars),
            "request_id": result.get("request_id", ""),
            "service_version": result.get("service_version", SERVICE_VERSION),
        }
        for opt_key in ("fallback_reason", "content_type", "page_count"):
            if opt_key in result and result[opt_key] is not None:
                payload[opt_key] = result[opt_key]
        return payload

    async def extract_webpage_text(
        url: Annotated[str, Field(description="Public HTTP or HTTPS URL of a webpage or PDF to extract plain text from.")],
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS, description="Maximum characters returned for content.")] = MCP_DEFAULT_MAX_CHARS,
        force_browser: Annotated[bool, Field(description="Start with browser rendering instead of fast HTTP retrieval.")] = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        result = await fetch_handler(url.strip(), force_browser, max_chars)
        text_content = result.get("content", "")
        payload: dict[str, Any] = {
            "success": bool(result.get("success", True)),
            "requested_url": result.get("requested_url", url.strip()),
            "final_url": result.get("final_url", url.strip()),
            "status_code": result.get("status_code", 200),
            "render_method": result.get("render_method", "browser" if force_browser else "http"),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "title": result.get("title", ""),
            "content": text_content,
            "truncated": bool(result.get("truncated", False)),
            "max_chars": result.get("max_chars", max_chars),
            "request_id": result.get("request_id", ""),
            "service_version": result.get("service_version", SERVICE_VERSION),
        }
        for opt_key in ("fallback_reason", "content_type", "page_count"):
            if opt_key in result and result[opt_key] is not None:
                payload[opt_key] = result[opt_key]
        return payload

    async def render_webpage(
        url: Annotated[str, Field(description="Public HTTP or HTTPS webpage URL for forced headless browser rendering.")],
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS, description="Maximum characters returned for content, Markdown, and HTML.")] = MCP_DEFAULT_MAX_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        result = await fetch_handler(url.strip(), True, max_chars, include_html=True)
        content = result.get("content", "")
        markdown = result.get("markdown", "")
        html_text = result.get("html", "")
        links = result.get("links", [])
        payload: dict[str, Any] = {
            "success": bool(result.get("success", True)),
            "requested_url": result.get("requested_url", url.strip()),
            "final_url": result.get("final_url", url.strip()),
            "status_code": result.get("status_code", 200),
            "render_method": "browser",
            "elapsed_ms": result.get("elapsed_ms", 0),
            "title": result.get("title", ""),
            "content": content,
            "markdown": markdown,
            "links": links,
            "word_count": result.get("word_count", len(content.split())),
            "content_hash": result.get("content_hash", "0" * 64),
            "low_quality": bool(result.get("low_quality", False)),
            "truncated": bool(result.get("truncated", False)),
            "original_content_chars": result.get("original_content_chars", len(content)),
            "original_markdown_chars": result.get("original_markdown_chars", len(markdown)),
            "returned_content_chars": result.get("returned_content_chars", len(content)),
            "returned_markdown_chars": result.get("returned_markdown_chars", len(markdown)),
            "links_returned": result.get("links_returned", len(links)),
            "max_chars": result.get("max_chars", max_chars),
            "request_id": result.get("request_id", ""),
            "service_version": result.get("service_version", SERVICE_VERSION),
            "html": html_text,
        }
        for opt_key in ("fallback_reason", "content_type", "page_count"):
            if opt_key in result and result[opt_key] is not None:
                payload[opt_key] = result[opt_key]
        return payload

    async def screenshot_webpage(
        url: Annotated[str, Field(description="Public HTTP or HTTPS URL of a webpage to capture as a screenshot.")],
        viewport: Annotated[str, Field(description="Device viewport preset ('desktop' for 1280x800, 'mobile' for 390x844).")] = "desktop",
        full_page: Annotated[bool, Field(description="Capture the full scrollable page height up to 4000px.")] = False,
        image_format: Annotated[str, Field(description="Output image format ('jpeg' or 'png').")] = "jpeg",
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        if viewport not in ("desktop", "mobile"):
            raise ValueError("viewport must be 'desktop' or 'mobile'")
        if image_format not in ("jpeg", "png"):
            raise ValueError("image_format must be 'jpeg' or 'png'")
        if screenshot_handler is not None:
            return await screenshot_handler(url.strip(), viewport, full_page, image_format)
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: browser_screenshot(url.strip(), viewport=viewport, full_page=full_page, image_format=image_format),
        )

    async def inspect_webpage(
        url: Annotated[str, Field(description="Public HTTP or HTTPS URL of a webpage to inspect.")],
        render_mode: Annotated[str, Field(description="Rendering strategy: 'auto' (HTTP with browser escalation on SPA shell), 'http_only', or 'browser'.")] = "auto",
        force_browser: Annotated[bool, Field(description="Force browser rendering bypassing HTTP tier.")] = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        if render_mode not in ("auto", "http_only", "browser"):
            raise ValueError("render_mode must be 'auto', 'http_only', or 'browser'")
        if inspect_handler is not None:
            return await inspect_handler(url.strip(), force_browser=force_browser, render_mode=render_mode)
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: inspect_webpage_url(url.strip(), force_browser=force_browser, render_mode=render_mode),
        )

    def protect(
        name: str,
        description: str,
        handler: Callable,
        extension: dict[str, Any] | None = None,
        tool_accepts: list[Any] | None = None,
        tool_price: str | None = None,
    ) -> Callable:
        price_str = tool_price or settings.price
        chosen_accepts = tool_accepts if tool_accepts is not None else accepts
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
                payment_amount=price_str,
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
                payment_amount=price_str,
            )

        payment_wrapper = create_payment_wrapper(
            resource_server,
            accepts=chosen_accepts,
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
            RENDER_OUTPUT_SCHEMA,
            RENDER_OUTPUT_EXAMPLE,
        ),
        (
            "screenshot_webpage",
            SCREENSHOT_DESCRIPTION,
            screenshot_webpage,
            SCREENSHOT_INPUT_SCHEMA,
            SCREENSHOT_INPUT_EXAMPLE,
            SCREENSHOT_OUTPUT_SCHEMA,
            SCREENSHOT_OUTPUT_EXAMPLE,
            screenshot_accepts,
            "$0.01",
        ),
        (
            "inspect_webpage",
            INSPECT_DESCRIPTION,
            inspect_webpage,
            INSPECT_INPUT_SCHEMA,
            INSPECT_INPUT_EXAMPLE,
            INSPECT_OUTPUT_SCHEMA,
            INSPECT_OUTPUT_EXAMPLE,
            accepts,
            settings.price,
        ),
    )

    remote_cfg = RemoteBrowserConfig.from_env()
    is_available = remote_cfg.is_configured

    for item in tools:
        name = item[0]
        description = item[1]
        handler = item[2]
        input_schema = item[3]
        input_example = item[4]
        output_schema = item[5]
        output_example = item[6]
        tool_accepts = item[7] if len(item) > 7 else accepts
        tool_price = item[8] if len(item) > 8 else settings.price
        extension = mcp_discovery_extension(
            tool_name=name,
            description=description,
            input_schema=input_schema,
            input_example=input_example,
            output_schema=output_schema,
            output_example=output_example,
            transport=MCP_TRANSPORT,
        )
        tool_handler = protect(name, description, handler, extension, tool_accepts=tool_accepts, tool_price=tool_price)
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

    async def _dummy_screenshot(url: str, viewport: str, full_page: bool, image_format: str) -> dict[str, Any]:
        return {
            "success": False,
            "error_code": "browser_unavailable",
            "error": "Browser is temporarily unavailable",
        }

    async def _dummy_inspect(url: str, render_mode: str = "auto", force_browser: bool = False) -> dict[str, Any]:
        return {
            "success": False,
            "error_code": "retrieval_unavailable",
            "error": "Retrieval is temporarily unavailable",
        }

    return create_smartfetch_mcp(
        settings,
        _dummy_fetch,
        screenshot_handler=_dummy_screenshot,
        inspect_handler=_dummy_inspect,
    )
