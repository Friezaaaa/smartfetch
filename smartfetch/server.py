"""SmartFetch ASGI application supporting remote MCP and HTTP retrieval with x402 payment protection."""

import asyncio
import os
import secrets

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .access_logging import uvicorn_log_config
from .config import HOST, PORT, SERVICE_NAME, SERVICE_VERSION
from .core import smart_fetch
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
from .mcp_server import MCP_PATH, MCP_TOOL, MCP_TOOLS, MCP_TRANSPORT, create_smartfetch_mcp, create_emergency_mcp
from .payments import install_x402, load_x402_settings
from .remote_browser import RemoteBrowserConfig
from .request_bounds import BoundedRequestMiddleware


_UNHEALTHY_BODY = b'{"success":false,"error_code":"service_unhealthy","error":"Service temporarily unavailable"}'


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
    ) -> dict:
        remote_cfg = RemoteBrowserConfig.from_env()
        if not remote_cfg.is_configured:
            raise RuntimeError("Browser rendering is unavailable: remote browser worker is not configured")
        loop = asyncio.get_running_loop()
        should_force = force_browser or os.getenv("BROWSER_WORKER_REQUIRED", "false").lower() in ("1", "true", "yes")
        result = await loop.run_in_executor(
            None,
            lambda: smart_fetch(
                url,
                should_force,
                max_chars,
                format=format,
                render_mode=render_mode,
                no_cache=no_cache,
            ),
        )
        result["request_id"] = secrets.token_hex(8)
        result["service_version"] = SERVICE_VERSION
        return result

    remote_cfg = RemoteBrowserConfig.from_env()
    if remote_cfg.is_configured:
        mcp = create_smartfetch_mcp(settings, _run_mcp_fetch)
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
        max_chars = int(body.get("max_chars", 20000))
        max_chars = max(1000, min(50000, max_chars))
        try:
            result = await _run_mcp_fetch(
                url.strip(),
                force_browser,
                max_chars,
                format=fmt,
                render_mode=render_mode,
                no_cache=no_cache,
            )
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            return JSONResponse(status_code=502, content={"success": False, "error_code": "fetch_failed", "error": str(exc)})

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
                "mcp": {
                    "url": urls["mcp"],
                    "transport": "streamable-http",
                    "tools": ["fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage"],
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
                        "price": settings.price,
                        "network": settings.network,
                        "asset": "USDC",
                    }
                    for name in MCP_TOOLS
                ],
            }
        return emergency_meta(urls)

    @application.get("/.well-known/x402")
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
                {"name": "fetch_webpage", "description": "Fetch webpage content and structure using isolated browser."},
                {"name": "webpage_to_markdown", "description": "Convert webpage content to Markdown using isolated browser."},
                {"name": "extract_webpage_text", "description": "Extract plain text from webpage using isolated browser."},
                {"name": "render_webpage", "description": "Render webpage with headless Chromium in isolated VPS worker."},
            ],
        }

    @application.get("/.well-known/glama.json")
    async def glama_ownership():
        return JSONResponse({
            "$schema": "https://glama.ai/mcp/schemas/connector.json",
            "maintainers": [{"email": "smartfetch.contact@gmail.com"}],
        })

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

    if settings.enabled and remote_cfg.is_configured:
        install_x402(application, settings)

    return BoundedRequestMiddleware(EmergencyDispatch(application))


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
