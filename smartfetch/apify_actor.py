"""Self-contained Apify Store Actor & Standby MCP Server for SmartFetch.

Supports both execution modes on the Apify platform:
1. Standard Run Mode:
   Reads Actor input (`action`: `fetch` | `screenshot` | `inspect` | `batch_fetch`),
   executes retrieval/inspection/screenshot locally, stores screenshot binary in the
   Apify Key-Value Store (`Actor.set_value`), pushes structured output to the default
   Dataset (`Actor.push_data`), and charges Pay-Per-Event (`Actor.charge`) strictly
   on successful results (zero charge on errors).
2. Standby MCP Mode (`APIFY_META_ORIGIN == "STANDBY"`):
   Starts a FastAPI + FastMCP Streamable HTTP server on `APIFY_ACTOR_STANDBY_PORT`
   (default 4321), responds `200 OK` to Apify's `x-apify-container-server-readiness-probe`
   at `GET /`, and exposes `fetch_webpage`, `screenshot_webpage`, `inspect_webpage`,
   and `batch_fetch_webpages` on `/mcp` with per-call Pay-Per-Event charging.
"""

import asyncio
import base64
from contextlib import asynccontextmanager
from functools import wraps
import json
import logging
import os
import secrets
from typing import Annotated, Any, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from starlette.routing import Route
import uvicorn

from .bazaar import (
    BATCH_FETCH_DESCRIPTION,
    FETCH_DESCRIPTION,
    INSPECT_DESCRIPTION,
    SCREENSHOT_DESCRIPTION,
)
from .batch import batch_fetch_urls
from .browser_fetch import browser_screenshot
from .config import MAX_REQUEST_BODY_BYTES, SERVICE_NAME, SERVICE_VERSION
from .core import smart_fetch
from .inspect import inspect_webpage_url
from .mcp_logging import configure_mcp_sdk_logging
from .server import MCPAcceptNormalization

_LOGGER = logging.getLogger("smartfetch.apify_actor")

EVENT_ACTOR_START = "actor-start"
EVENT_PAGE_FETCHED = "page-fetched"
EVENT_SITE_INSPECTED = "site-inspected"
EVENT_SCREENSHOT_CAPTURED = "screenshot-captured"

FREE_PLAN_MAX_BATCH_URLS = 3
DEFAULT_STANDBY_PORT = 4321


def _get_actor_cls(actor_cls: Any = None) -> Any:
    if actor_cls is not None:
        return actor_cls
    from apify import Actor  # type: ignore[import-not-found]

    return Actor


def is_user_paying(environ: Mapping[str, str] | None = None) -> bool:
    """Checks whether the Apify user is on a paid plan via APIFY_USER_IS_PAYING."""
    env = os.environ if environ is None else environ
    raw = str(env.get("APIFY_USER_IS_PAYING", "")).strip().lower()
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    if str(env.get("APIFY_IS_AT_HOME", "")).strip() == "1":
        return False
    return True


async def _charge_event(actor: Any, event_name: str, count: int = 1) -> None:
    """Charges an Apify Pay-Per-Event only when count > 0 and Actor.charge is available."""
    if count <= 0 or actor is None:
        return
    charge_fn = getattr(actor, "charge", None)
    if callable(charge_fn):
        await charge_fn(event_name=event_name, count=int(count))


async def execute_action(
    actor_input: dict[str, Any],
    *,
    actor: Any = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Executes a single Apify Standard Run action, pushing data and charging on success."""
    action = str(actor_input.get("action") or "fetch").strip().lower()
    url = str(actor_input.get("url") or "").strip()
    raw_urls = actor_input.get("urls")
    max_chars = int(actor_input.get("max_chars") or 20000)
    max_chars = max(1000, min(50000, max_chars))
    render_mode = str(actor_input.get("render_mode") or "auto").strip().lower()
    if render_mode not in ("auto", "http_only", "browser"):
        render_mode = "auto"
    viewport = str(actor_input.get("viewport") or "desktop").strip().lower()
    if viewport not in ("desktop", "mobile"):
        viewport = "desktop"
    full_page = bool(actor_input.get("full_page", False))
    image_format = str(actor_input.get("image_format") or "jpeg").strip().lower()
    if image_format not in ("jpeg", "png"):
        image_format = "jpeg"
    no_cache = bool(actor_input.get("no_cache", False))

    loop = asyncio.get_running_loop()

    if action == "fetch":
        if not url:
            err_item = {
                "action": "fetch",
                "success": False,
                "requested_url": url,
                "error_code": "invalid_url",
                "error": "url must be a non-empty string",
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item
        try:
            res = await loop.run_in_executor(
                None,
                lambda: smart_fetch(
                    url,
                    force_browser=(render_mode == "browser"),
                    max_chars=max_chars,
                    render_mode=render_mode,
                    no_cache=no_cache,
                ),
            )
            res["action"] = "fetch"
            res.setdefault("request_id", secrets.token_hex(8))
            res.setdefault("service_version", SERVICE_VERSION)
            if res.get("success"):
                await _charge_event(actor, EVENT_PAGE_FETCHED, count=1)
            if actor is not None:
                await actor.push_data(res)
            return res
        except Exception as exc:
            err_item = {
                "action": "fetch",
                "success": False,
                "requested_url": url,
                "error_code": "fetch_failed",
                "error": str(exc),
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item

    if action == "screenshot":
        if not url:
            err_item = {
                "action": "screenshot",
                "success": False,
                "requested_url": url,
                "error_code": "invalid_url",
                "error": "url must be a non-empty string",
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item
        try:
            res = await loop.run_in_executor(
                None,
                lambda: browser_screenshot(
                    url,
                    viewport=viewport,
                    full_page=full_page,
                    image_format=image_format,
                    no_cache=no_cache,
                ),
            )
            res["action"] = "screenshot"
            res.setdefault("request_id", secrets.token_hex(8))
            res.setdefault("service_version", SERVICE_VERSION)
            if res.get("success") and res.get("image_base64"):
                ext = "png" if res.get("image_format") == "png" else "jpg"
                kv_key = f"screenshot.{ext}"
                mime_type = res.get("mime_type") or ("image/png" if ext == "png" else "image/jpeg")
                image_bytes = base64.b64decode(res["image_base64"])
                if actor is not None and hasattr(actor, "set_value"):
                    await actor.set_value(kv_key, image_bytes, content_type=mime_type)
                res["screenshot_key"] = kv_key
                await _charge_event(actor, EVENT_SCREENSHOT_CAPTURED, count=1)
            if actor is not None:
                await actor.push_data(res)
            return res
        except Exception as exc:
            err_item = {
                "action": "screenshot",
                "success": False,
                "requested_url": url,
                "error_code": "screenshot_failed",
                "error": str(exc),
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item

    if action == "inspect":
        if not url:
            err_item = {
                "action": "inspect",
                "success": False,
                "requested_url": url,
                "error_code": "invalid_url",
                "error": "url must be a non-empty string",
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item
        try:
            res = await loop.run_in_executor(
                None,
                lambda: inspect_webpage_url(
                    url,
                    force_browser=(render_mode == "browser"),
                    render_mode=render_mode,
                    no_cache=no_cache,
                ),
            )
            res["action"] = "inspect"
            res.setdefault("request_id", secrets.token_hex(8))
            res.setdefault("service_version", SERVICE_VERSION)
            if res.get("success"):
                await _charge_event(actor, EVENT_SITE_INSPECTED, count=1)
            if actor is not None:
                await actor.push_data(res)
            return res
        except Exception as exc:
            err_item = {
                "action": "inspect",
                "success": False,
                "requested_url": url,
                "error_code": "inspect_failed",
                "error": str(exc),
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item

    if action == "batch_fetch":
        urls: list[str] = []
        if isinstance(raw_urls, (list, tuple)):
            urls = [str(u).strip() for u in raw_urls if isinstance(u, str) and str(u).strip()]
        elif url:
            urls = [url]

        if not urls:
            err_item = {
                "action": "batch_fetch",
                "success": False,
                "urls": [],
                "error_code": "invalid_urls",
                "error": "urls must be a non-empty list of URL strings",
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item

        paying = is_user_paying(environ)
        free_plan_batch_capped = False
        if not paying and len(urls) > FREE_PLAN_MAX_BATCH_URLS:
            urls = urls[:FREE_PLAN_MAX_BATCH_URLS]
            free_plan_batch_capped = True

        batch_render_mode = "http_only" if render_mode == "http_only" else "auto"
        batch_max_chars = max(1000, min(20000, max_chars))
        try:
            res = await loop.run_in_executor(
                None,
                lambda: batch_fetch_urls(
                    urls,
                    max_chars_per_url=batch_max_chars,
                    render_mode=batch_render_mode,
                    no_cache=no_cache,
                ),
            )
            res["action"] = "batch_fetch"
            res["free_plan_batch_capped"] = free_plan_batch_capped
            if free_plan_batch_capped:
                res["max_urls_allowed"] = FREE_PLAN_MAX_BATCH_URLS
            succeeded_count = int(res.get("succeeded_count", 0))
            if res.get("success") and succeeded_count > 0:
                await _charge_event(actor, EVENT_PAGE_FETCHED, count=succeeded_count)
            if actor is not None:
                await actor.push_data(res)
            return res
        except Exception as exc:
            err_item = {
                "action": "batch_fetch",
                "success": False,
                "urls": urls,
                "free_plan_batch_capped": free_plan_batch_capped,
                "error_code": "batch_fetch_failed",
                "error": str(exc),
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item

    err_item = {
        "action": action,
        "success": False,
        "error_code": "invalid_action",
        "error": f"Unsupported action '{action}'. Must be fetch, screenshot, inspect, or batch_fetch.",
    }
    if actor is not None:
        await actor.push_data(err_item)
    return err_item


async def run_standard_mode(
    actor_cls: Any = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Runs the Apify Actor in standard one-shot mode."""
    actor = _get_actor_cls(actor_cls)
    if hasattr(actor, "__aenter__"):
        async with actor:
            actor_input = (await actor.get_input()) or {}
            return await execute_action(actor_input, actor=actor, environ=environ)
    actor_input = (await actor.get_input()) or {}
    return await execute_action(actor_input, actor=actor, environ=environ)


def create_standby_app(
    actor_cls: Any = None,
    *,
    environ: Mapping[str, str] | None = None,
):
    """Builds the FastAPI + FastMCP Streamable HTTP app for Apify Standby Mode."""
    mcp = FastMCP(
        SERVICE_NAME,
        host="0.0.0.0",
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
    )
    mcp._mcp_server.version = SERVICE_VERSION
    configure_mcp_sdk_logging()

    def _resolve_actor() -> Any:
        try:
            return _get_actor_cls(actor_cls)
        except Exception:
            return None

    def _wrap_mcp_tool(handler):
        @wraps(handler)
        async def _wrapped(**kwargs):
            try:
                res = await handler(**kwargs)
                text = json.dumps(res, ensure_ascii=False)
                return CallToolResult(
                    content=[TextContent(type="text", text=text)],
                    structuredContent=res,
                    isError=not bool(res.get("success", True)),
                )
            except Exception as exc:
                err_msg = str(exc)
                err_payload = {"success": False, "error_code": "tool_execution_failed", "error": err_msg}
                return CallToolResult(
                    content=[TextContent(type="text", text=err_msg)],
                    structuredContent=err_payload,
                    isError=True,
                )

        return _wrapped

    async def fetch_webpage(
        url: Annotated[str, Field(description="Public HTTP or HTTPS URL of a webpage or PDF to retrieve.")],
        max_chars: Annotated[int, Field(ge=1000, le=50000, description="Maximum characters returned for content and Markdown.")] = 20000,
        force_browser: Annotated[bool, Field(description="Start with browser rendering instead of fast HTTP retrieval.")] = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None,
            lambda: smart_fetch(url.strip(), force_browser=force_browser, max_chars=max_chars),
        )
        res.setdefault("request_id", secrets.token_hex(8))
        res.setdefault("service_version", SERVICE_VERSION)
        if res.get("success"):
            await _charge_event(_resolve_actor(), EVENT_PAGE_FETCHED, count=1)
        return res

    async def screenshot_webpage(
        url: Annotated[str, Field(description="Public HTTP or HTTPS URL of a webpage to capture as a screenshot.")],
        viewport: Annotated[str, Field(description="Device viewport preset ('desktop' or 'mobile').")] = "desktop",
        full_page: Annotated[bool, Field(description="Capture the full scrollable page height up to 4000px.")] = False,
        image_format: Annotated[str, Field(description="Output image format ('jpeg' or 'png').")] = "jpeg",
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        if viewport not in ("desktop", "mobile"):
            raise ValueError("viewport must be 'desktop' or 'mobile'")
        if image_format not in ("jpeg", "png"):
            raise ValueError("image_format must be 'jpeg' or 'png'")
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None,
            lambda: browser_screenshot(
                url.strip(),
                viewport=viewport,
                full_page=full_page,
                image_format=image_format,
            ),
        )
        res.setdefault("request_id", secrets.token_hex(8))
        res.setdefault("service_version", SERVICE_VERSION)
        if res.get("success"):
            await _charge_event(_resolve_actor(), EVENT_SCREENSHOT_CAPTURED, count=1)
        return res

    async def inspect_webpage(
        url: Annotated[str, Field(description="Public HTTP or HTTPS URL of a webpage to inspect.")],
        render_mode: Annotated[str, Field(description="Rendering strategy: 'auto', 'http_only', or 'browser'.")] = "auto",
        force_browser: Annotated[bool, Field(description="Force browser rendering bypassing HTTP tier.")] = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        if render_mode not in ("auto", "http_only", "browser"):
            raise ValueError("render_mode must be 'auto', 'http_only', or 'browser'")
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None,
            lambda: inspect_webpage_url(
                url.strip(),
                force_browser=force_browser,
                render_mode=render_mode,
            ),
        )
        res.setdefault("request_id", secrets.token_hex(8))
        res.setdefault("service_version", SERVICE_VERSION)
        if res.get("success"):
            await _charge_event(_resolve_actor(), EVENT_SITE_INSPECTED, count=1)
        return res

    async def batch_fetch_webpages(
        urls: Annotated[list[str], Field(description="List of 1 to 5 public HTTP/HTTPS URLs to retrieve in parallel.")],
        max_chars_per_url: Annotated[int, Field(ge=1000, le=20000, description="Maximum characters returned per URL.")] = 8000,
        render_mode: Annotated[str, Field(description="Rendering strategy: 'auto' or 'http_only'.")] = "auto",
        no_cache: Annotated[bool, Field(description="Bypass cache lookup.")] = False,
    ) -> dict[str, Any]:
        if not isinstance(urls, (list, tuple)) or not urls:
            raise ValueError("urls must be a non-empty list of URL strings")
        if render_mode not in ("auto", "http_only"):
            raise ValueError("render_mode must be 'auto' or 'http_only'")
        target_urls = list(urls)
        paying = is_user_paying(environ)
        free_plan_batch_capped = False
        if not paying and len(target_urls) > FREE_PLAN_MAX_BATCH_URLS:
            target_urls = target_urls[:FREE_PLAN_MAX_BATCH_URLS]
            free_plan_batch_capped = True
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None,
            lambda: batch_fetch_urls(
                target_urls,
                max_chars_per_url=max_chars_per_url,
                render_mode=render_mode,
                no_cache=no_cache,
            ),
        )
        res["free_plan_batch_capped"] = free_plan_batch_capped
        succeeded_count = int(res.get("succeeded_count", 0))
        if res.get("success") and succeeded_count > 0:
            await _charge_event(_resolve_actor(), EVENT_PAGE_FETCHED, count=succeeded_count)
        return res

    for tool_name, tool_desc, tool_fn in (
        ("fetch_webpage", FETCH_DESCRIPTION, fetch_webpage),
        ("screenshot_webpage", SCREENSHOT_DESCRIPTION, screenshot_webpage),
        ("inspect_webpage", INSPECT_DESCRIPTION, inspect_webpage),
        ("batch_fetch_webpages", BATCH_FETCH_DESCRIPTION, batch_fetch_webpages),
    ):
        mcp.tool(name=tool_name, description=tool_desc, structured_output=False)(_wrap_mcp_tool(tool_fn))

    http_app = mcp.streamable_http_app()
    mcp_route = next(
        candidate
        for candidate in http_app.routes
        if isinstance(candidate, Route) and candidate.path == "/mcp"
    )

    @asynccontextmanager
    async def _lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title=f"{SERVICE_NAME} Apify Standby MCP",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.router.routes.append(mcp_route)

    @app.get("/")
    async def standby_root(request: Request):
        is_probe = "x-apify-container-server-readiness-probe" in request.headers
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "mode": "apify_standby_mcp",
                "readiness_probe": is_probe,
                "mcp_path": "/mcp",
                "tools": [
                    "fetch_webpage",
                    "screenshot_webpage",
                    "inspect_webpage",
                    "batch_fetch_webpages",
                ],
            },
        )

    @app.get("/health")
    async def standby_health(request: Request):
        is_probe = "x-apify-container-server-readiness-probe" in request.headers
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "mode": "apify_standby_mcp",
                "readiness_probe": is_probe,
            },
        )

    return MCPAcceptNormalization(app)


async def run_standby_mode(actor_cls: Any = None) -> None:
    """Starts the Apify Standby HTTP + MCP server inside Actor context."""
    actor = _get_actor_cls(actor_cls)
    port = int(os.getenv("APIFY_ACTOR_STANDBY_PORT") or os.getenv("ACTOR_WEB_SERVER_PORT") or DEFAULT_STANDBY_PORT)
    app = create_standby_app(actor_cls=actor)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    if hasattr(actor, "__aenter__"):
        async with actor:
            await server.serve()
    else:
        await server.serve()


def main() -> None:
    origin = str(os.getenv("APIFY_META_ORIGIN", "")).strip().upper()
    if origin == "STANDBY":
        asyncio.run(run_standby_mode())
    else:
        asyncio.run(run_standard_mode())


if __name__ == "__main__":
    main()
