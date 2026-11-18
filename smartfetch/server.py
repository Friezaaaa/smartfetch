"""SmartFetch ASGI application supporting remote MCP and HTTP retrieval with x402 payment protection."""

import asyncio
import os
import secrets

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .access_logging import uvicorn_log_config
from .batch import batch_fetch_urls
from .bazaar import (
    BATCH_FETCH_DESCRIPTION,
    BATCH_FETCH_TAGS,
    FETCH_DESCRIPTION,
    INSPECT_DESCRIPTION,
    INSPECT_TAGS,
    LEADS_DESCRIPTION,
    LEADS_TAGS,
    MAP_DESCRIPTION,
    MAP_TAGS,
    MARKDOWN_DESCRIPTION,
    RENDER_DESCRIPTION,
    SCREENSHOT_DESCRIPTION,
    SCREENSHOT_TAGS,
    SEARCH_AND_FETCH_DESCRIPTION,
    SEARCH_AND_FETCH_TAGS,
    SEARCH_DESCRIPTION,
    SEARCH_TAGS,
    TEXT_DESCRIPTION,
    X_SEARCH_DESCRIPTION,
    X_SEARCH_TAGS,
    batch_fetch_discovery_extension,
    inspect_discovery_extension,
    leads_discovery_extension,
    map_discovery_extension,
    screenshot_discovery_extension,
    search_and_fetch_discovery_extension,
    search_discovery_extension,
    x_search_discovery_extension,
)
from .browser_fetch import browser_screenshot
from .config import HOST, PORT, SERVICE_NAME, SERVICE_VERSION
from .core import smart_fetch
from .diagnostics import RetrievalFailure
from .inspect import inspect_webpage_url
from .leads import leads_configured, search_leads_query
from .mapper import map_website_urls
from .search_tools import search_and_fetch_query, search_configured, search_web_query
from .social import search_x_posts_query, x_search_configured
from .discovery import (
    agent_card_document,
    docs_html,
    emergency_docs_html,
    emergency_llms_text,
    emergency_meta,
    emergency_openapi_document,
    emergency_x402_manifest,
    llms_text,
    openapi_document,
    owners_document,
    public_urls,
    robots_text,
    sitemap_xml,
    x402_manifest,
)
from .emergency_dispatch import EmergencyDispatch
from .mcp_server import MCP_PATH, MCP_TOOL, MCP_TOOLS, MCP_TRANSPORT, create_smartfetch_mcp, create_emergency_mcp, get_enabled_mcp_tools
from .payments import install_x402, load_x402_settings
from .remote_browser import RemoteBrowserConfig
from .request_bounds import BoundedRequestMiddleware
from starlette.middleware.cors import CORSMiddleware


_UNHEALTHY_BODY = b'{"success":false,"error_code":"service_unhealthy","error":"Service temporarily unavailable"}'
_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    b'<circle cx="50" cy="50" r="48" fill="#090d16" stroke="#38bdf8" stroke-width="4"/>'
    b'<path d="M30 45 L50 25 L70 45 M50 25 L50 75" stroke="#38bdf8" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>'
    b'</svg>'
)


class SmartFetchCORSMiddleware(CORSMiddleware):
    """Wraps CORSMiddleware with attribute delegation to keep application state accessible."""

    def __getattr__(self, name):
        return getattr(self.app, name)


class MCPAcceptNormalization:
    """Normalizes missing or single-media-type Accept headers on POST /mcp to satisfy FastMCP Streamable HTTP transport."""

    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/mcp"
        ):
            headers = list(scope.get("headers", []))
            accept_values = [v for k, v in headers if k.lower() == b"accept"]
            raw_accept = b", ".join(accept_values).decode("latin1", "replace").strip().lower()
            unique_types = {t.split(";")[0].strip() for t in raw_accept.split(",") if t.strip()}

            if not unique_types or unique_types in ({"*/*"}, {"application/json"}, {"text/event-stream"}):
                filtered_headers = [(k, v) for k, v in headers if k.lower() != b"accept"]
                filtered_headers.append((b"accept", b"application/json, text/event-stream"))
                scope = dict(scope)
                scope["headers"] = filtered_headers

        return await self.app(scope, receive, send)


def create_app(payment_settings=None, *, v111_runtime=None, v111_environ=None):
    """Build the production SmartFetch application with x402 payment protection and isolated browser routing."""
    settings = load_x402_settings(v111_environ) if payment_settings is None else payment_settings

    async def _run_mcp_fetch(
        url: str,
        force_browser: bool,
        max_chars: int,
        *,
        format: str = "markdown",
        render_mode: str = "auto",
        no_cache: bool = False,
        include_html: bool = False,
        only_main_content: bool = True,
        wait_ms: int = 0,
        wait_for_selector: str | None = None,
        scroll: bool = False,
        dismiss_cookies: bool = True,
    ) -> dict:
        loop = asyncio.get_running_loop()
        should_force = force_browser
        extra_kwargs: dict[str, Any] = {}
        if not only_main_content:
            extra_kwargs["only_main_content"] = False
        if wait_ms or wait_for_selector is not None or scroll or not dismiss_cookies:
            extra_kwargs.update({
                "wait_ms": wait_ms,
                "wait_for_selector": wait_for_selector,
                "scroll": scroll,
                "dismiss_cookies": dismiss_cookies,
            })
        result = await loop.run_in_executor(
            None,
            lambda: smart_fetch(
                url,
                should_force,
                max_chars,
                format=format,
                render_mode=render_mode,
                no_cache=no_cache,
                include_html=include_html,
                **extra_kwargs,
            ),
        )
        result["request_id"] = secrets.token_hex(8)
        result["service_version"] = SERVICE_VERSION
        return result

    async def _run_screenshot(
        url: str,
        viewport: str = "desktop",
        full_page: bool = False,
        image_format: str = "jpeg",
        *,
        no_cache: bool = False,
        wait_ms: int = 0,
        wait_for_selector: str | None = None,
        scroll: bool = False,
        dismiss_cookies: bool = True,
    ) -> dict:
        loop = asyncio.get_running_loop()
        extra_kwargs: dict[str, Any] = {}
        if wait_ms or wait_for_selector is not None or scroll or not dismiss_cookies:
            extra_kwargs.update({
                "wait_ms": wait_ms,
                "wait_for_selector": wait_for_selector,
                "scroll": scroll,
                "dismiss_cookies": dismiss_cookies,
            })
        result = await loop.run_in_executor(
            None,
            lambda: browser_screenshot(
                url,
                viewport=viewport,
                full_page=full_page,
                image_format=image_format,
                no_cache=no_cache,
                **extra_kwargs,
            ),
        )
        result["request_id"] = secrets.token_hex(8)
        result["service_version"] = SERVICE_VERSION
        return result

    async def _run_inspect(
        url: str,
        *,
        force_browser: bool = False,
        render_mode: str = "auto",
        no_cache: bool = False,
        deep: bool = True,
    ) -> dict:
        loop = asyncio.get_running_loop()
        inspect_kwargs: dict[str, Any] = {
            "force_browser": force_browser,
            "render_mode": render_mode,
            "no_cache": no_cache,
        }
        if not deep:
            inspect_kwargs["deep"] = False
        result = await loop.run_in_executor(
            None,
            lambda: inspect_webpage_url(url, **inspect_kwargs),
        )
        result["request_id"] = secrets.token_hex(8)
        result["service_version"] = SERVICE_VERSION
        return result

    async def _run_batch_fetch(
        urls: list[str],
        *,
        max_chars_per_url: int = 8000,
        render_mode: str = "auto",
        no_cache: bool = False,
    ) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: batch_fetch_urls(
                urls,
                max_chars_per_url=max_chars_per_url,
                render_mode=render_mode,
                no_cache=no_cache,
            ),
        )

    async def _run_map(
        url: str,
        *,
        search: str | None = None,
        include_subdomains: bool = False,
        max_urls: int = 200,
        no_cache: bool = False,
    ) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: map_website_urls(
                url,
                search=search,
                include_subdomains=include_subdomains,
                max_urls=max_urls,
                no_cache=no_cache,
            ),
        )

    remote_cfg = RemoteBrowserConfig.from_env()
    if remote_cfg.is_configured:
        mcp = create_smartfetch_mcp(
            settings,
            _run_mcp_fetch,
            screenshot_handler=_run_screenshot,
            inspect_handler=_run_inspect,
            batch_fetch_handler=_run_batch_fetch,
            map_handler=_run_map,
        )
    else:
        mcp = create_emergency_mcp()
    application = FastAPI(
        title=SERVICE_NAME,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=mcp.lifespan,
    )
    application.router.routes.append(mcp.route)
    application.state.smartfetch_mcp = mcp
    application.state.terminal_unhealthy = False

    @application.get("/health")
    async def health():
        if application.state.terminal_unhealthy:
            return Response(
                content=_UNHEALTHY_BODY,
                status_code=503,
                headers={
                    "content-type": "application/json; charset=utf-8",
                    "retry-after": "3600",
                    "cache-control": "no-store",
                },
            )
        remote_cfg = RemoteBrowserConfig.from_env()
        if remote_cfg.is_configured:
            return {
                "ok": True,
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "retrieval_available": True,
                "browser_available": True,
                "mode": "isolated_browser",
            }
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "retrieval_available": False,
            "browser_available": False,
            "mode": "emergency_fail_closed",
        }

    @application.post("/fetch")
    async def fetch(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "retrieval_unavailable", "error": "Retrieval is temporarily unavailable"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_url", "error": "URL must be a non-empty string"})
        force_browser = bool(body.get("force_browser", False))
        render_mode = str(body.get("render_mode", "auto"))
        fmt = str(body.get("format", "markdown"))
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        only_main_content = bool(body.get("only_main_content", True))
        max_chars = int(body.get("max_chars", 20000))
        max_chars = max(1000, min(50000, max_chars))
        from .browser_pool import normalize_capture_options
        wait_ms, wait_for_selector, scroll, dismiss_cookies = normalize_capture_options(
            wait_ms=body.get("wait_ms", 0),
            wait_for_selector=body.get("wait_for_selector"),
            scroll=body.get("scroll", False),
            dismiss_cookies=body.get("dismiss_cookies", True),
        )
        try:
            extra_kwargs: dict[str, Any] = {}
            if not only_main_content:
                extra_kwargs["only_main_content"] = False
            if wait_ms or wait_for_selector is not None or scroll or not dismiss_cookies:
                extra_kwargs.update({
                    "wait_ms": wait_ms,
                    "wait_for_selector": wait_for_selector,
                    "scroll": scroll,
                    "dismiss_cookies": dismiss_cookies,
                })
            result = await _run_mcp_fetch(
                url.strip(),
                force_browser,
                max_chars,
                format=fmt,
                render_mode=render_mode,
                no_cache=no_cache,
                **extra_kwargs,
            )
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "fetch_failed", "error": str(exc)})

    @application.post("/screenshot")
    async def screenshot_endpoint(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "browser_unavailable", "error": "Browser is temporarily unavailable"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_url", "error": "URL must be a non-empty string"})
        viewport = str(body.get("viewport", "desktop"))
        if viewport not in ("desktop", "mobile"):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_viewport", "error": "Viewport must be 'desktop' or 'mobile'"})
        full_page = bool(body.get("full_page", False))
        image_format = str(body.get("image_format", "jpeg"))
        if image_format not in ("jpeg", "png"):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_image_format", "error": "Image format must be 'jpeg' or 'png'"})
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        from .browser_pool import normalize_capture_options
        wait_ms, wait_for_selector, scroll, dismiss_cookies = normalize_capture_options(
            wait_ms=body.get("wait_ms", 0),
            wait_for_selector=body.get("wait_for_selector"),
            scroll=body.get("scroll", False),
            dismiss_cookies=body.get("dismiss_cookies", True),
        )
        try:
            extra_kwargs: dict[str, Any] = {}
            if wait_ms or wait_for_selector is not None or scroll or not dismiss_cookies:
                extra_kwargs.update({
                    "wait_ms": wait_ms,
                    "wait_for_selector": wait_for_selector,
                    "scroll": scroll,
                    "dismiss_cookies": dismiss_cookies,
                })
            result = await _run_screenshot(
                url.strip(),
                viewport=viewport,
                full_page=full_page,
                image_format=image_format,
                no_cache=no_cache,
                **extra_kwargs,
            )
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "screenshot_failed", "error": str(exc)})

    @application.post("/inspect")
    async def inspect_endpoint(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "retrieval_unavailable", "error": "Retrieval is temporarily unavailable"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_url", "error": "URL must be a non-empty string"})
        render_mode = str(body.get("render_mode", "auto"))
        if render_mode not in ("auto", "http_only", "browser"):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_render_mode", "error": "render_mode must be 'auto', 'http_only', or 'browser'"})
        force_browser = bool(body.get("force_browser", False))
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        deep = bool(body.get("deep", True))
        try:
            inspect_kwargs: dict[str, Any] = {
                "force_browser": force_browser,
                "render_mode": render_mode,
                "no_cache": no_cache,
            }
            if not deep:
                inspect_kwargs["deep"] = False
            result = await _run_inspect(url.strip(), **inspect_kwargs)
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "inspect_failed", "error": str(exc)})

    @application.post("/batch-fetch")
    async def batch_fetch_endpoint(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "retrieval_unavailable", "error": "Retrieval is temporarily unavailable"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        urls = body.get("urls")
        if not isinstance(urls, (list, tuple)) or not urls:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_urls", "error": "urls must be a non-empty list of strings"})
        max_chars = body.get("max_chars_per_url", 8000)
        try:
            max_chars = int(max_chars)
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_max_chars", "error": "max_chars_per_url must be an integer"})
        render_mode = str(body.get("render_mode", "auto"))
        if render_mode not in ("auto", "http_only"):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_render_mode", "error": "render_mode must be 'auto' or 'http_only'"})
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        try:
            result = await _run_batch_fetch(
                urls,
                max_chars_per_url=max_chars,
                render_mode=render_mode,
                no_cache=no_cache,
            )
            return JSONResponse(status_code=200, content=result)
        except RetrievalFailure as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "batch_retrieval_failed", "error": str(exc)})
        except Exception as exc:
            return JSONResponse(
                status_code=422 if ("urls" in str(exc).lower() or "batch" in str(exc).lower()) else 502,
                content={"success": False, "error_code": "batch_fetch_failed", "error": str(exc)},
            )

    @application.post("/map")
    async def map_endpoint(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "retrieval_unavailable", "error": "Retrieval is temporarily unavailable"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_url", "error": "URL must be a non-empty string"})
        search = body.get("search")
        if search is not None and not isinstance(search, str):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_search", "error": "search must be a string"})
        include_subdomains = bool(body.get("include_subdomains", False))
        max_urls_raw = body.get("max_urls", 200)
        try:
            max_urls = max(1, min(500, int(max_urls_raw)))
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_max_urls", "error": "max_urls must be an integer"})
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        try:
            result = await _run_map(
                url.strip(),
                search=search,
                include_subdomains=include_subdomains,
                max_urls=max_urls,
                no_cache=no_cache,
            )
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "map_failed", "error": str(exc)})

    @application.post("/search")
    async def search_endpoint(request: Request):
        if not search_configured():
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "provider_unavailable", "error": "Optional provider API key is not configured"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        query = body.get("query")
        if not isinstance(query, str) or not (1 <= len(query.strip()) <= 500):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_query", "error": "query must be a string between 1 and 500 characters"})
        provider = str(body.get("provider", "auto"))
        search_type = str(body.get("search_type", "web"))
        try:
            num_results = max(1, min(10, int(body.get("num_results", 10))))
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_num_results", "error": "num_results must be an integer"})
        country = str(body.get("country", "us"))
        language = str(body.get("language", "en"))
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: search_web_query(
                    query.strip(),
                    provider=provider,
                    search_type=search_type,
                    num_results=num_results,
                    country=country,
                    language=language,
                    no_cache=no_cache,
                ),
            )
            return JSONResponse(status_code=200, content=result)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "search_failed", "error": str(exc)})

    @application.post("/search-and-fetch")
    async def search_and_fetch_endpoint(request: Request):
        if not search_configured():
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "provider_unavailable", "error": "Optional provider API key is not configured"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "retrieval_unavailable", "error": "Retrieval is temporarily unavailable"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        query = body.get("query")
        if not isinstance(query, str) or not (1 <= len(query.strip()) <= 500):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_query", "error": "query must be a string between 1 and 500 characters"})
        try:
            num_results = max(1, min(5, int(body.get("num_results", 3))))
            max_chars_per_page = max(1000, min(15000, int(body.get("max_chars_per_page", 6000))))
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid numeric parameter"})
        provider = str(body.get("provider", "auto"))
        render_mode = str(body.get("render_mode", "auto"))
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: search_and_fetch_query(
                    query.strip(),
                    num_results=num_results,
                    max_chars_per_page=max_chars_per_page,
                    provider=provider,
                    render_mode=render_mode,
                    no_cache=no_cache,
                ),
            )
            return JSONResponse(status_code=200, content=result)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "search_and_fetch_failed", "error": str(exc)})

    @application.post("/leads")
    async def leads_endpoint(request: Request):
        if not leads_configured():
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "provider_unavailable", "error": "Optional provider API key is not configured"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        query = body.get("query")
        if not isinstance(query, str) or not (1 <= len(query.strip()) <= 500):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_query", "error": "query must be a string between 1 and 500 characters"})
        source = str(body.get("source", "maps"))
        try:
            max_leads = max(1, min(5, int(body.get("max_leads", 5))))
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_max_leads", "error": "max_leads must be an integer"})
        deep = bool(body.get("deep", True))
        country = str(body.get("country", "us"))
        language = str(body.get("language", "en"))
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: search_leads_query(
                    query.strip(),
                    source=source,
                    max_leads=max_leads,
                    deep=deep,
                    country=country,
                    language=language,
                    no_cache=no_cache,
                ),
            )
            return JSONResponse(status_code=200, content=result)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "leads_failed", "error": str(exc)})

    @application.post("/x-search")
    async def x_search_endpoint(request: Request):
        if not x_search_configured():
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_code": "provider_unavailable", "error": "Optional provider API key is not configured"},
                headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_json", "error": "Invalid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": "Invalid request"})
        query = body.get("query")
        if not isinstance(query, str) or not (1 <= len(query.strip()) <= 500):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_query", "error": "query must be a string between 1 and 500 characters"})
        query_type = str(body.get("query_type", "Latest"))
        try:
            max_tweets = max(1, min(20, int(body.get("max_tweets", 10))))
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_max_tweets", "error": "max_tweets must be an integer"})
        cursor = body.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_cursor", "error": "cursor must be a string or null"})
        no_cache = bool(body.get("no_cache", False)) or request.headers.get("cache-control") == "no-cache"
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: search_x_posts_query(
                    query.strip(),
                    query_type=query_type,
                    max_tweets=max_tweets,
                    cursor=cursor,
                    no_cache=no_cache,
                ),
            )
            return JSONResponse(status_code=200, content=result)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"success": False, "error_code": "invalid_request", "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "x_search_failed", "error": str(exc)})

    @application.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        urls = public_urls(request)
        if remote_cfg.is_configured:
            return docs_html(urls, settings)
        return emergency_docs_html(urls)

    @application.get("/meta")
    async def meta(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        urls = public_urls(request)
        if remote_cfg.is_configured:
            return {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "mode": "isolated_browser",
                "retrieval_available": True,
                "browser_available": True,
                "fetch": {
                    "url": urls["fetch"],
                    "availability": "available",
                    "payment_challenge_enabled": settings.enabled,
                },
                "screenshot": {
                    "url": urls["screenshot"],
                    "availability": "available",
                    "payment_challenge_enabled": settings.enabled,
                    "price": "$0.01",
                },
                "inspect": {
                    "url": urls["inspect"],
                    "availability": "available",
                    "payment_challenge_enabled": settings.enabled,
                    "price": "$0.005",
                },
                "batch_fetch": {
                    "url": urls["batch_fetch"],
                    "availability": "available",
                    "payment_challenge_enabled": settings.enabled,
                    "price": "$0.015",
                },
                "map": {
                    "url": urls["map"],
                    "availability": "available",
                    "payment_challenge_enabled": settings.enabled,
                    "price": "$0.005",
                },
                **({
                    "search": {
                        "url": urls["search"],
                        "availability": "available",
                        "payment_challenge_enabled": settings.enabled,
                        "price": "$0.01",
                    },
                    "search_and_fetch": {
                        "url": urls["search_and_fetch"],
                        "availability": "available",
                        "payment_challenge_enabled": settings.enabled,
                        "price": "$0.02",
                    },
                } if search_configured() else {}),
                **({
                    "leads": {
                        "url": urls["leads"],
                        "availability": "available",
                        "payment_challenge_enabled": settings.enabled,
                        "price": "$0.03",
                    },
                } if leads_configured() else {}),
                **({
                    "x_search": {
                        "url": urls["x_search"],
                        "availability": "available",
                        "payment_challenge_enabled": settings.enabled,
                        "price": "$0.008",
                    },
                } if x_search_configured() else {}),
                "enabled_features": {
                    "search": search_configured(),
                    "search_and_fetch": search_configured(),
                    "leads": leads_configured(),
                    "x_search": x_search_configured(),
                },
                "mcp": {
                    "url": urls["mcp"],
                    "transport": "streamable-http",
                    "tools": list(get_enabled_mcp_tools()),
                },
                "discovery": {
                    "x402": urls["x402"],
                    "docs": urls["docs"],
                    "openapi": urls["openapi"],
                    "llms": urls["llms"],
                    "robots": urls["robots"],
                    "sitemap": urls["sitemap"],
                },
                "tools": [
                    {
                        "name": name,
                        "availability": "available",
                        "payment_challenge_enabled": settings.enabled,
                        "price": (
                            "$0.03" if name == "search_leads"
                            else "$0.02" if name == "search_and_fetch"
                            else "$0.015" if name == "batch_fetch_webpages"
                            else "$0.01" if name in ("screenshot_webpage", "search_web")
                            else "$0.008" if name == "search_x_posts"
                            else "$0.005" if name in ("inspect_webpage", "map_website")
                            else settings.price
                        ),
                        "network": settings.network,
                        "asset": "USDC",
                        **({
                            "solana_network": settings.solana_network,
                            "networks": [settings.network, settings.solana_network],
                        } if settings.solana_pay_to and settings.solana_network else {}),
                    }
                    for name in get_enabled_mcp_tools()
                ],
                **({
                    "solana_network": settings.solana_network,
                    "networks": [settings.network, settings.solana_network],
                } if settings.solana_pay_to and settings.solana_network else {}),
            }
        return emergency_meta(urls)

    @application.get("/.well-known/x402")
    @application.get("/.well-known/x402.json")
    async def x402_manifest_endpoint(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        urls = public_urls(request)
        if remote_cfg.is_configured:
            return x402_manifest(urls, settings)
        return emergency_x402_manifest(urls)

    @application.get("/.well-known/mcp.json")
    async def mcp_discovery(request: Request):
        urls = public_urls(request)
        return {
            "name": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "transport": "streamable-http",
            "url": urls["mcp"],
            "tools": [
                {"name": "fetch_webpage", "description": FETCH_DESCRIPTION},
                {"name": "webpage_to_markdown", "description": MARKDOWN_DESCRIPTION},
                {"name": "extract_webpage_text", "description": TEXT_DESCRIPTION},
                {"name": "render_webpage", "description": RENDER_DESCRIPTION},
                {"name": "screenshot_webpage", "description": SCREENSHOT_DESCRIPTION},
                {"name": "inspect_webpage", "description": INSPECT_DESCRIPTION},
                {"name": "batch_fetch_webpages", "description": BATCH_FETCH_DESCRIPTION},
                {"name": "map_website", "description": MAP_DESCRIPTION},
                *([
                    {"name": "search_web", "description": SEARCH_DESCRIPTION},
                    {"name": "search_and_fetch", "description": SEARCH_AND_FETCH_DESCRIPTION},
                ] if search_configured() else []),
                *([
                    {"name": "search_leads", "description": LEADS_DESCRIPTION},
                ] if leads_configured() else []),
                *([
                    {"name": "search_x_posts", "description": X_SEARCH_DESCRIPTION},
                ] if x_search_configured() else []),
            ],
        }

    @application.get("/.well-known/glama.json")
    async def glama_ownership():
        return JSONResponse({
            "$schema": "https://glama.ai/mcp/schemas/connector.json",
            "maintainers": [{"email": "smartfetch.contact@gmail.com"}],
        })

    @application.get("/.well-known/402index-verify.txt", response_class=PlainTextResponse)
    async def index402_domain_verification():
        return PlainTextResponse(
            os.environ.get(
                "INDEX402_VERIFY_HASH",
                "7544f08648ac863080cbd821e8cb34add313ecc58d238d7217e498fd08a80e02",
            )
        )

    @application.get("/.well-known/agent-card.json")
    async def agent_card_discovery(request: Request):
        urls = public_urls(request)
        return JSONResponse(agent_card_document(urls, settings))

    @application.get("/.well-known/owners.json")
    async def owners_discovery(request: Request):
        urls = public_urls(request)
        return JSONResponse(owners_document(urls))

    @application.get("/mcp/.well-known/owners.json")
    async def mcp_owners_discovery(request: Request):
        urls = public_urls(request)
        return JSONResponse(owners_document(urls))

    @application.get("/openapi.json")
    async def openapi(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        urls = public_urls(request)
        if remote_cfg.is_configured:
            return openapi_document(urls, settings)
        return emergency_openapi_document(urls)

    @application.get("/docs", response_class=HTMLResponse)
    async def docs(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        urls = public_urls(request)
        if remote_cfg.is_configured:
            return docs_html(urls, settings)
        return emergency_docs_html(urls)

    @application.get("/llms.txt", response_class=PlainTextResponse)
    async def llms(request: Request):
        remote_cfg = RemoteBrowserConfig.from_env()
        urls = public_urls(request)
        if remote_cfg.is_configured:
            return llms_text(urls, settings)
        return emergency_llms_text(urls)

    @application.get("/robots.txt", response_class=PlainTextResponse)
    async def robots(request: Request):
        return robots_text(public_urls(request))

    @application.get("/sitemap.xml")
    async def sitemap(request: Request):
        return Response(content=sitemap_xml(public_urls(request)), media_type="application/xml")

    @application.get("/favicon.ico")
    async def favicon_ico():
        return Response(
            content=_FAVICON_SVG,
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @application.get("/favicon.svg")
    async def favicon_svg():
        return Response(
            content=_FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    if settings.enabled and remote_cfg.is_configured:
        from x402.http.types import RouteConfig
        from .payments import build_payment_options

        screenshot_options = build_payment_options(settings, "$0.01")
        inspect_options = build_payment_options(settings, "$0.005")
        batch_fetch_options = build_payment_options(settings, "$0.015")
        map_options = build_payment_options(settings, "$0.005")

        extra_routes = {
            "POST /screenshot": RouteConfig(
                accepts=screenshot_options if len(screenshot_options) > 1 else screenshot_options[0],
                description=SCREENSHOT_DESCRIPTION,
                mime_type="application/json",
                service_name="SmartFetch",
                tags=SCREENSHOT_TAGS,
                extensions=screenshot_discovery_extension(),
            ),
            "POST /inspect": RouteConfig(
                accepts=inspect_options if len(inspect_options) > 1 else inspect_options[0],
                description=INSPECT_DESCRIPTION,
                mime_type="application/json",
                service_name="SmartFetch",
                tags=INSPECT_TAGS,
                extensions=inspect_discovery_extension(),
            ),
            "POST /batch-fetch": RouteConfig(
                accepts=batch_fetch_options if len(batch_fetch_options) > 1 else batch_fetch_options[0],
                description=BATCH_FETCH_DESCRIPTION,
                mime_type="application/json",
                service_name="SmartFetch",
                tags=BATCH_FETCH_TAGS,
                extensions=batch_fetch_discovery_extension(),
            ),
            "POST /map": RouteConfig(
                accepts=map_options if len(map_options) > 1 else map_options[0],
                description=MAP_DESCRIPTION,
                mime_type="application/json",
                service_name="SmartFetch",
                tags=MAP_TAGS,
                extensions=map_discovery_extension(),
            ),
        }
        if search_configured():
            search_options = build_payment_options(settings, "$0.01")
            search_and_fetch_options = build_payment_options(settings, "$0.02")
            extra_routes["POST /search"] = RouteConfig(
                accepts=search_options if len(search_options) > 1 else search_options[0],
                description=SEARCH_DESCRIPTION,
                mime_type="application/json",
                service_name="SmartFetch",
                tags=SEARCH_TAGS,
                extensions=search_discovery_extension(),
            )
            extra_routes["POST /search-and-fetch"] = RouteConfig(
                accepts=search_and_fetch_options if len(search_and_fetch_options) > 1 else search_and_fetch_options[0],
                description=SEARCH_AND_FETCH_DESCRIPTION,
                mime_type="application/json",
                service_name="SmartFetch",
                tags=SEARCH_AND_FETCH_TAGS,
                extensions=search_and_fetch_discovery_extension(),
            )
        if leads_configured():
            leads_options = build_payment_options(settings, "$0.03")
            extra_routes["POST /leads"] = RouteConfig(
                accepts=leads_options if len(leads_options) > 1 else leads_options[0],
                description=LEADS_DESCRIPTION,
                mime_type="application/json",
                service_name="SmartFetch",
                tags=LEADS_TAGS,
                extensions=leads_discovery_extension(),
            )
        if x_search_configured():
            x_search_options = build_payment_options(settings, "$0.008")
            extra_routes["POST /x-search"] = RouteConfig(
                accepts=x_search_options if len(x_search_options) > 1 else x_search_options[0],
                description=X_SEARCH_DESCRIPTION,
                mime_type="application/json",
                service_name="SmartFetch",
                tags=X_SEARCH_TAGS,
                extensions=x_search_discovery_extension(),
            )
        install_x402(application, settings, additional_routes=extra_routes)

    cors_wrapped = SmartFetchCORSMiddleware(
        EmergencyDispatch(MCPAcceptNormalization(application)),
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Accept",
            "Cache-Control",
            "PAYMENT-SIGNATURE",
            "X-PAYMENT",
            "Mcp-Session-Id",
            "Last-Event-ID",
        ],
        expose_headers=[
            "PAYMENT-REQUIRED",
            "PAYMENT-RESPONSE",
            "X-Request-Id",
            "X-SmartFetch-Availability",
            "Mcp-Session-Id",
        ],
    )
    return BoundedRequestMiddleware(cors_wrapped)


app = create_app()


def create_uvicorn_config(application=None) -> uvicorn.Config:
    forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS")
    if forwarded_allow_ips is None and os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        forwarded_allow_ips = "*"
    return uvicorn.Config(
        app if application is None else application,
        host=HOST,
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        log_config=uvicorn_log_config(),
    )


def main():
    print(
        f"{SERVICE_NAME} {SERVICE_VERSION} listening on http://{HOST}:{PORT}",
        flush=True,
    )
    uvicorn.Server(create_uvicorn_config()).run()


if __name__ == "__main__":
    main()
