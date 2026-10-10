"""Public HTTP behavior of the emergency fail-closed application."""

import importlib
import os
import re
import socket
import subprocess
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


ORDINARY_BODY = b'{"success":false,"error_code":"retrieval_unavailable","error":"Retrieval is temporarily unavailable"}'
BROWSER_BODY = b'{"success":false,"error_code":"browser_unavailable","error":"Browser rendering is temporarily unavailable"}'


def _assert_shared_headers(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert re.fullmatch(r"[0-9a-f]{16}", response.headers["x-request-id"])
    for name in (b"cache-control", b"x-content-type-options", b"x-request-id"):
        assert sum(key.lower() == name for key, _ in response.headers.raw) == 1


@pytest.mark.parametrize("path", [
    "/health", "/", "/meta", "/openapi.json", "/docs", "/llms.txt",
    "/.well-known/x402", "/.well-known/glama.json", "/robots.txt", "/sitemap.xml",
])
def test_free_responses_preserve_generated_security_and_cache_headers(path):
    _, app = _app()
    with (
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        patch.object(socket, "create_connection", side_effect=AssertionError("network entered")) as network,
        patch.object(subprocess, "Popen", side_effect=AssertionError("worker entered")) as worker,
        TestClient(app) as client,
    ):
        response = client.get(path, headers={"X-Request-ID": "client-chosen-canary"})
    assert response.status_code == 200
    _assert_shared_headers(response)
    assert response.headers["x-request-id"] != "client-chosen-canary"
    dns.assert_not_called()
    network.assert_not_called()
    worker.assert_not_called()


def _app():
    safe_env = {"SystemRoot": os.environ["SystemRoot"]} if "SystemRoot" in os.environ else {}
    with patch.dict(os.environ, safe_env, clear=True):
        server = importlib.import_module("smartfetch.server")
        from smartfetch.payments import X402Settings

        app = server.create_app(X402Settings(False, None, "$0.005", "eip155:8453"))
    return server, app


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"url": "https://example.com/private-canary"}, ORDINARY_BODY),
        ({"url": "https://example.com/private-canary", "force_browser": True}, BROWSER_BODY),
    ],
)
def test_valid_fetch_is_unavailable_before_retrieval_or_payment(payload, expected):
    server, app = _app()
    work = AsyncMock(side_effect=AssertionError("retrieval boundary entered"))
    with patch.object(server, "_run_fetch", work, create=True):
        with TestClient(app) as client:
            response = client.post(
                "/fetch",
                json=payload,
                headers={"PAYMENT-SIGNATURE": "secret-payment-canary"},
            )

    assert response.status_code == 503
    assert response.content == expected
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["retry-after"] == "3600"
    assert response.headers["cache-control"] == "no-store"
    _assert_shared_headers(response)
    assert all("payment" not in name.lower() for name in response.headers)
    assert b"private-canary" not in response.content
    assert b"secret-payment-canary" not in response.content
    work.assert_not_awaited()


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        (b"{", "application/json", 400),
        (b"[]", "application/json", 400),
        (b'{"url":123}', "application/json", 422),
        (b'{"url":"https://example.com/"}', "text/plain", 415),
        (b"x" * 20_001, "application/json", 400),
    ],
)
def test_invalid_fetch_is_not_relabelled_unavailable(body, content_type, status):
    _, app = _app()
    with TestClient(app) as client:
        response = client.post(
            "/fetch",
            content=body,
            headers={"content-type": content_type, "PAYMENT-SIGNATURE": "proof-canary"},
        )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    _assert_shared_headers(response)
    assert "payment-required" not in response.headers
    assert "payment-response" not in response.headers
    assert b"proof-canary" not in response.content


def test_get_and_head_fetch_remain_method_not_allowed():
    _, app = _app()
    with TestClient(app) as client:
        get_result = client.get("/fetch")
        head_result = client.head("/fetch")
    assert get_result.status_code == head_result.status_code == 405
    assert get_result.headers["allow"] == head_result.headers["allow"] == "POST"
    _assert_shared_headers(get_result)
    _assert_shared_headers(head_result)
    assert head_result.content == b""


def test_get_fetch_preserves_machine_guidance_and_discloses_emergency_unavailability():
    import socket
    import x402.http.middleware.fastapi as official_http
    import x402.server as official_server

    _, app = _app()
    with (
        patch.object(official_http, "payment_middleware", wraps=official_http.payment_middleware) as wrapper,
        patch.object(official_server.x402ResourceServer, "verify_payment", wraps=official_server.x402ResourceServer.verify_payment) as verify,
        patch.object(official_server.x402ResourceServer, "settle_payment", wraps=official_server.x402ResourceServer.settle_payment) as settle,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        TestClient(app) as client,
    ):
        response = client.get("/fetch", headers={"PAYMENT-SIGNATURE": "private-proof-canary"})
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) <= 256
    guidance = response.json()
    assert guidance["error"] == "method_not_allowed"
    assert guidance["openapi"] == "/openapi.json"
    assert guidance["documentation"] == "/docs"
    assert guidance["payment_discovery"] == "/.well-known/x402"
    assert guidance["retrieval_available"] is False
    assert guidance["mode"] == "emergency_fail_closed"
    assert b"private-proof-canary" not in response.content
    assert all("payment" not in name.lower() for name in response.headers)
    for boundary in (wrapper, verify, settle, dns):
        boundary.assert_not_called()


def test_head_fetch_preserves_empty_guidance_and_machine_unavailability_header():
    import socket
    import x402.http.middleware.fastapi as official_http
    import x402.server as official_server

    _, app = _app()
    with (
        patch.object(official_http, "payment_middleware", wraps=official_http.payment_middleware) as wrapper,
        patch.object(official_server.x402ResourceServer, "verify_payment", wraps=official_server.x402ResourceServer.verify_payment) as verify,
        patch.object(official_server.x402ResourceServer, "settle_payment", wraps=official_server.x402ResourceServer.settle_payment) as settle,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        TestClient(app) as client,
    ):
        response = client.head("/fetch", headers={"PAYMENT-SIGNATURE": "private-proof-canary"})
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["x-smartfetch-availability"] == "unavailable"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b""
    assert all("payment" not in name.lower() for name in response.headers)
    for boundary in (wrapper, verify, settle, dns):
        boundary.assert_not_called()


@pytest.mark.parametrize("path", [
    "/search-and-extract/results",
    "/search-and-extract/answer",
    "/search-and-extract/structured",
    "/extract-structured-data/webpage",
    "/extract-structured-data/image",
    "/extract-structured-data/pdf",
    "/extract-structured-data/audio",
    "/extract-structured-data/video",
])
def test_all_eight_v111_paths_are_ordinary_404(path):
    _, app = _app()
    with TestClient(app) as client:
        response = client.post(path, json={})
    assert response.status_code == 404
    assert "payment-required" not in response.headers


def test_all_create_app_entrypoints_ignore_activation_and_perform_no_external_work():
    import concurrent.futures
    import socket
    import subprocess
    import tempfile

    server, _ = _app()
    with (
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        patch.object(socket, "create_connection", side_effect=AssertionError("socket entered")) as connect,
        patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess entered")) as proc,
        patch.object(tempfile, "mkdtemp", side_effect=AssertionError("workspace entered")) as temp,
        patch.object(concurrent.futures.ThreadPoolExecutor, "submit", side_effect=AssertionError("worker entered")) as worker,
    ):
        app = server.create_app(
            payment_settings=object(),
            v111_runtime=object(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )
        with TestClient(app) as client:
            response = client.post("/fetch", json={"url": "https://example.com/fallback"})
    assert response.status_code == 503
    for forbidden in (dns, connect, proc, temp, worker):
        forbidden.assert_not_called()
    assert not hasattr(server, "_run_fetch")
    assert not hasattr(server, "_run_mcp_fetch")
