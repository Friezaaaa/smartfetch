"""Public HTTP behavior of the emergency fail-closed application."""

import importlib
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


ORDINARY_BODY = b'{"success":false,"error_code":"retrieval_unavailable","error":"Retrieval is temporarily unavailable"}'
BROWSER_BODY = b'{"success":false,"error_code":"browser_unavailable","error":"Browser rendering is temporarily unavailable"}'


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
    assert head_result.content == b""


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
