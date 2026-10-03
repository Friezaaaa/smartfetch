"""Emergency-only SmartFetch ASGI application.

This module deliberately constructs no retrieval, browser, provider or payment
executor. Retrieval requests are intercepted by the outer ASGI boundary.
"""

import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .access_logging import uvicorn_log_config
from .config import HOST, PORT, SERVICE_NAME, SERVICE_VERSION
from .discovery import (
    emergency_docs_html,
    emergency_llms_text,
    emergency_meta,
    emergency_openapi_document,
    emergency_x402_manifest,
    public_urls,
    robots_text,
    sitemap_xml,
)
from .emergency_dispatch import EmergencyDispatch
from .mcp_server import create_emergency_mcp
from .request_bounds import BoundedRequestMiddleware


_UNHEALTHY_BODY = b'{"success":false,"error_code":"service_unhealthy","error":"Service temporarily unavailable"}'


def create_app(payment_settings=None, *, v111_runtime=None, v111_environ=None):
    """Retain caller compatibility without permitting an activation override."""
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
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "retrieval_available": False,
            "browser_available": False,
            "mode": "emergency_fail_closed",
        }

    @application.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        return emergency_docs_html(public_urls(request))

    @application.get("/meta")
    async def meta(request: Request):
        return emergency_meta(public_urls(request))

    @application.get("/.well-known/x402")
    async def x402_manifest(request: Request):
        return emergency_x402_manifest(public_urls(request))

    @application.get("/.well-known/glama.json")
    async def glama_ownership():
        return JSONResponse({
            "$schema": "https://glama.ai/mcp/schemas/connector.json",
            "maintainers": [{"email": "smartfetch.contact@gmail.com"}],
        })

    @application.get("/openapi.json")
    async def openapi(request: Request):
        return emergency_openapi_document(public_urls(request))

    @application.get("/docs", response_class=HTMLResponse)
    async def docs(request: Request):
        return emergency_docs_html(public_urls(request))

    @application.get("/llms.txt", response_class=PlainTextResponse)
    async def llms(request: Request):
        return emergency_llms_text(public_urls(request))

    @application.get("/robots.txt", response_class=PlainTextResponse)
    async def robots(request: Request):
        return robots_text(public_urls(request))

    @application.get("/sitemap.xml")
    async def sitemap(request: Request):
        return Response(content=sitemap_xml(public_urls(request)), media_type="application/xml")

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
