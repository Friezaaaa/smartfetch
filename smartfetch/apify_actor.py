"""Self-contained Apify Store Actor & Standby MCP Server for SmartFetch.

Supports both execution modes on the Apify platform:
1. Standard Run Mode:
   Reads Actor input (`action`: `fetch` | `screenshot` | `inspect` | `batch_fetch` | `bulk_inspect` | `map`),
   executes retrieval/inspection/screenshot/mapping locally, stores screenshot binary in the
   Apify Key-Value Store (`Actor.set_value`), pushes structured output to the default
   Dataset (`Actor.push_data`), and charges Pay-Per-Event (`Actor.charge`) strictly
   on successful results (zero charge on errors).
2. Standby MCP Mode (`APIFY_META_ORIGIN == "STANDBY"`):
   Starts a FastAPI + FastMCP Streamable HTTP server on `APIFY_ACTOR_STANDBY_PORT`
   (default 4321), responds `200 OK` to Apify's `x-apify-container-server-readiness-probe`
   at `GET /`, and exposes `fetch_webpage`, `screenshot_webpage`, `inspect_webpage`,
   `batch_fetch_webpages`, and `map_website` on `/mcp` with per-call Pay-Per-Event charging.
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import wraps
import json
import logging
import os
import secrets
from typing import Annotated, Any, Mapping
from urllib.parse import urlparse

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
    MAP_DESCRIPTION,
    SCREENSHOT_DESCRIPTION,
)
from .batch import batch_fetch_urls
from .browser_fetch import browser_screenshot
from .config import MAX_REQUEST_BODY_BYTES, SERVICE_NAME, SERVICE_VERSION
from .core import smart_fetch
from .inspect import inspect_webpage_url
from .mapper import map_website_urls
from .mcp_logging import configure_mcp_sdk_logging
from .server import MCPAcceptNormalization

_LOGGER = logging.getLogger("smartfetch.apify_actor")

EVENT_ACTOR_START = "actor-start"
EVENT_PAGE_FETCHED = "page-fetched"
EVENT_SITE_INSPECTED = "site-inspected"
EVENT_SCREENSHOT_CAPTURED = "screenshot-captured"

FREE_PLAN_MAX_BATCH_URLS = 3
FREE_PLAN_MAX_BULK_INSPECT_URLS = 5
PAID_MAX_BULK_INSPECT_URLS = 50
BULK_INSPECT_MAX_WORKERS = 5
DEFAULT_STANDBY_PORT = 4321


def _normalize_url_or_domain(raw: str) -> str:
    """Prepends https:// if the user supplied a bare domain like stripe.com."""
    cleaned = str(raw or "").strip()
    if not cleaned:
        return ""
    if not cleaned.lower().startswith(("http://", "https://")):
        return f"https://{cleaned}"
    return cleaned


def _flatten_inspect_row(target_url: str, res: dict[str, Any] | None, error: str | None = None) -> dict[str, Any]:
    """Formats an inspect_webpage_url result into a flat, CSV/Excel-ready Dataset row."""
    parsed = urlparse(target_url)
    domain = (parsed.hostname or target_url).lower()
    if error or not res or not res.get("success"):
        err_msg = error or (res.get("error") if isinstance(res, dict) else "Inspection failed")
        return {
            "action": "bulk_inspect",
            "domain": domain,
            "url": target_url,
            "requested_url": target_url,
            "final_url": res.get("final_url", target_url) if isinstance(res, dict) else target_url,
            "success": False,
            "status_code": res.get("status_code") if isinstance(res, dict) else None,
            "title": None,
            "description": None,
            "emails": "",
            "phones": "",
            "github": None,
            "linkedin": None,
            "x_twitter": None,
            "tech_stack": "",
            "https": target_url.lower().startswith("https://"),
            "has_hsts": False,
            "has_csp": False,
            "error": str(err_msg) if err_msg else "Inspection failed",
        }

    final_url = str(res.get("final_url") or target_url)
    final_host = urlparse(final_url).hostname
    if final_host:
        domain = final_host.lower()
    meta = res.get("metadata") or {}
    contacts = res.get("contacts") or {}
    social = contacts.get("social_links") or {}
    sec = res.get("security_headers") or {}
    emails_list = contacts.get("emails") or []
    phones_list = contacts.get("phones") or []
    tech_list = res.get("tech_stack") or []

    return {
        "action": "bulk_inspect",
        "domain": domain,
        "url": final_url,
        "requested_url": target_url,
        "final_url": final_url,
        "success": True,
        "status_code": res.get("status_code", 200),
        "title": meta.get("title"),
        "description": meta.get("description"),
        "emails": ", ".join(str(e) for e in emails_list),
        "phones": ", ".join(str(p) for p in phones_list),
        "github": social.get("github"),
        "linkedin": social.get("linkedin"),
        "x_twitter": social.get("x_twitter"),
        "tech_stack": ", ".join(str(t) for t in tech_list),
        "https": bool(sec.get("https", final_url.lower().startswith("https://"))),
        "has_hsts": bool(sec.get("strict_transport_security")),
        "has_csp": bool(sec.get("content_security_policy")),
        "error": None,
    }


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

    if action == "bulk_inspect":
        raw_items: list[str] = []
        if isinstance(raw_urls, (list, tuple)):
            raw_items = [str(u).strip() for u in raw_urls if isinstance(u, str) and str(u).strip()]
        elif url:
            raw_items = [url]

        normalized_urls: list[str] = []
        seen_urls: set[str] = set()
        for item in raw_items:
            norm = _normalize_url_or_domain(item)
            if norm and norm not in seen_urls:
                seen_urls.add(norm)
                normalized_urls.append(norm)

        if not normalized_urls:
            err_item = {
                "action": "bulk_inspect",
                "success": False,
                "urls": [],
                "error_code": "invalid_urls",
                "error": "urls must be a non-empty list of URLs or domains",
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item

        paying = is_user_paying(environ)
        max_allowed = PAID_MAX_BULK_INSPECT_URLS if paying else FREE_PLAN_MAX_BULK_INSPECT_URLS
        free_plan_batch_capped = False
        if len(normalized_urls) > max_allowed:
            normalized_urls = normalized_urls[:max_allowed]
            if not paying:
                free_plan_batch_capped = True

        def _inspect_one(target_url: str) -> dict[str, Any]:
            try:
                res_one = inspect_webpage_url(
                    target_url,
                    force_browser=(render_mode == "browser"),
                    render_mode=render_mode,
                    no_cache=no_cache,
                )
                return _flatten_inspect_row(target_url, res_one)
            except Exception as exc:
                return _flatten_inspect_row(target_url, None, error=str(exc))

        def _run_all_bulk_inspect() -> list[dict[str, Any]]:
            with ThreadPoolExecutor(max_workers=min(BULK_INSPECT_MAX_WORKERS, len(normalized_urls))) as pool:
                return list(pool.map(_inspect_one, normalized_urls))

        rows = await loop.run_in_executor(None, _run_all_bulk_inspect)
        succeeded_count = sum(1 for r in rows if r.get("success"))
        failed_count = len(rows) - succeeded_count

        if actor is not None:
            for row in rows:
                await actor.push_data(row)

        if succeeded_count > 0:
            await _charge_event(actor, EVENT_SITE_INSPECTED, count=succeeded_count)

        return {
            "action": "bulk_inspect",
            "success": succeeded_count > 0,
            "total_urls": len(rows),
            "succeeded_count": succeeded_count,
            "failed_count": failed_count,
            "free_plan_batch_capped": free_plan_batch_capped,
            "max_urls_allowed": max_allowed,
            "results": rows,
        }

    if action == "map":
        norm_url = _normalize_url_or_domain(url)
        if not norm_url:
            err_item = {
                "action": "map",
                "success": False,
                "requested_url": url,
                "error_code": "invalid_url",
                "error": "url must be a non-empty string",
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item
        search_val = actor_input.get("search")
        search_str = str(search_val).strip() if isinstance(search_val, str) and search_val.strip() else None
        include_subdomains = bool(actor_input.get("include_subdomains", False))
        max_urls_val = int(actor_input.get("max_urls") or 200)
        max_urls_val = max(1, min(500, max_urls_val))
        try:
            res = await loop.run_in_executor(
                None,
                lambda: map_website_urls(
                    norm_url,
                    search=search_str,
                    include_subdomains=include_subdomains,
                    max_urls=max_urls_val,
                    no_cache=no_cache,
                ),
            )
            res["action"] = "map"
            res.setdefault("request_id", secrets.token_hex(8))
            res.setdefault("service_version", SERVICE_VERSION)
            if res.get("success"):
                await _charge_event(actor, EVENT_PAGE_FETCHED, count=1)
            if actor is not None:
                await actor.push_data(res)
            return res
        except Exception as exc:
            err_item = {
                "action": "map",
                "success": False,
                "requested_url": norm_url,
                "error_code": "map_failed",
                "error": str(exc),
            }
            if actor is not None:
                await actor.push_data(err_item)
            return err_item

    err_item = {
        "action": action,
        "success": False,
        "error_code": "invalid_action",
        "error": f"Unsupported action '{action}'. Must be fetch, screenshot, inspect, batch_fetch, bulk_inspect, or map.",
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

    async def map_website(
        url: Annotated[str, Field(description="Public HTTP or HTTPS root/starting URL to map.")],
        search: Annotated[str | None, Field(description="Optional case-insensitive substring filter for discovered URLs.")] = None,
        include_subdomains: Annotated[bool, Field(description="Include URLs on subdomains of the target domain.")] = False,
        max_urls: Annotated[int, Field(ge=1, le=500, description="Maximum number of URLs to return (1-500).")] = 200,
        no_cache: Annotated[bool, Field(description="Bypass cache lookup.")] = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None,
            lambda: map_website_urls(
                url.strip(),
                search=search,
                include_subdomains=include_subdomains,
                max_urls=max_urls,
                no_cache=no_cache,
            ),
        )
        res.setdefault("request_id", secrets.token_hex(8))
        res.setdefault("service_version", SERVICE_VERSION)
        if res.get("success"):
            await _charge_event(_resolve_actor(), EVENT_PAGE_FETCHED, count=1)
        return res

    for tool_name, tool_desc, tool_fn in (
        ("fetch_webpage", FETCH_DESCRIPTION, fetch_webpage),
        ("screenshot_webpage", SCREENSHOT_DESCRIPTION, screenshot_webpage),
        ("inspect_webpage", INSPECT_DESCRIPTION, inspect_webpage),
        ("batch_fetch_webpages", BATCH_FETCH_DESCRIPTION, batch_fetch_webpages),
        ("map_website", MAP_DESCRIPTION, map_website),
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
                    "map_website",
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
