"""Emergency-only SmartFetch ASGI application.

This module deliberately constructs no retrieval, browser, provider or payment
executor. Retrieval requests are intercepted by the outer ASGI boundary.
"""

import os

import uvicorn
from fastapi import FastAPI

from .access_logging import uvicorn_log_config
from .config import HOST, PORT, SERVICE_NAME, SERVICE_VERSION
from .emergency_dispatch import EmergencyDispatch
from .mcp_server import create_emergency_mcp
from .request_bounds import BoundedRequestMiddleware


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

    @application.get("/health")
    async def health():
        return {"ok": True, "service": SERVICE_NAME, "version": SERVICE_VERSION}

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
