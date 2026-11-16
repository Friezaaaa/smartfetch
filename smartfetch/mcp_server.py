"""Unwrapped, native MCP identities for SmartFetch service."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import os
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from starlette.routing import Route

from .config import HOST, MAX_REQUEST_BODY_BYTES, SERVICE_VERSION
from .mcp_logging import configure_mcp_sdk_logging
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
    mcp._mcp_server.version = SERVICE_VERSION
    configure_mcp_sdk_logging()

    def _execute_sync(url: str, force_browser: bool, max_chars: int) -> dict[str, Any]:
        from .core import smart_fetch
        return smart_fetch(url, force_browser=force_browser, max_chars=max_chars)

    async def fetch_webpage(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> CallToolResult:
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return _unavailable(force_browser)
        try:
            loop = asyncio.get_running_loop()
            should_force = force_browser or os.getenv("BROWSER_WORKER_REQUIRED", "false").lower() in ("1", "true", "yes")
            result = await loop.run_in_executor(None, _execute_sync, url, should_force, max_chars)
            return CallToolResult(
                content=[TextContent(type="text", text=result.get("content", "") or result.get("markdown", "") or result.get("html", ""))],
                structuredContent=result,
                isError=False,
            )
        except Exception as exc:
            err_msg = str(exc)
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=err_msg)],
                structuredContent={"success": False, "error_code": "render_failed", "error": err_msg},
            )

    async def webpage_to_markdown(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> CallToolResult:
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return _unavailable(force_browser)
        try:
            loop = asyncio.get_running_loop()
            should_force = force_browser or os.getenv("BROWSER_WORKER_REQUIRED", "false").lower() in ("1", "true", "yes")
            result = await loop.run_in_executor(None, _execute_sync, url, should_force, max_chars)
            md_text = result.get("markdown", "") or result.get("content", "")
            payload = {
                "success": True,
                "requested_url": url,
                "final_url": result.get("final_url", url),
                "status_code": result.get("status_code", 200),
                "render_method": result.get("render_method", "browser"),
                "elapsed_ms": result.get("elapsed_ms", 0),
                "title": result.get("title", ""),
                "markdown": md_text,
            }
            return CallToolResult(
                content=[TextContent(type="text", text=md_text)],
                structuredContent=payload,
                isError=False,
            )
        except Exception as exc:
            err_msg = str(exc)
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=err_msg)],
                structuredContent={"success": False, "error_code": "render_failed", "error": err_msg},
            )

    async def extract_webpage_text(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> CallToolResult:
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return _unavailable(force_browser)
        try:
            loop = asyncio.get_running_loop()
            should_force = force_browser or os.getenv("BROWSER_WORKER_REQUIRED", "false").lower() in ("1", "true", "yes")
            result = await loop.run_in_executor(None, _execute_sync, url, should_force, max_chars)
            text_content = result.get("content", "")
            payload = {
                "success": True,
                "requested_url": url,
                "final_url": result.get("final_url", url),
                "status_code": result.get("status_code", 200),
                "render_method": result.get("render_method", "browser"),
                "elapsed_ms": result.get("elapsed_ms", 0),
                "title": result.get("title", ""),
                "content": text_content,
            }
            return CallToolResult(
                content=[TextContent(type="text", text=text_content)],
                structuredContent=payload,
                isError=False,
            )
        except Exception as exc:
            err_msg = str(exc)
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=err_msg)],
                structuredContent={"success": False, "error_code": "render_failed", "error": err_msg},
            )

    async def render_webpage(
        url: str,
        max_chars: Annotated[int, Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS)] = MCP_DEFAULT_MAX_CHARS,
    ) -> CallToolResult:
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return _unavailable(True)
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _execute_sync, url, True, max_chars)
            html_text = result.get("html", "") or result.get("content", "")
            payload = {
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
            return CallToolResult(
                content=[TextContent(type="text", text=html_text)],
                structuredContent=payload,
                isError=False,
            )
        except Exception as exc:
            err_msg = str(exc)
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=err_msg)],
                structuredContent={"success": False, "error_code": "render_failed", "error": err_msg},
            )

    remote_cfg = RemoteBrowserConfig.from_env()
    is_available = remote_cfg.is_configured
    for name, description, handler in (
        (MCP_TOOL, "Fetch webpage content and structure using isolated browser.", fetch_webpage),
        ("webpage_to_markdown", "Convert webpage content to Markdown using isolated browser.", webpage_to_markdown),
        ("extract_webpage_text", "Extract plain text from webpage using isolated browser.", extract_webpage_text),
        ("render_webpage", "Render webpage with headless Chromium in isolated VPS worker.", render_webpage),
    ):
        mcp.tool(
            name=name,
            description=description,
            meta={"availability": "available" if is_available else "unavailable", "payment_challenge_enabled": False},
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
