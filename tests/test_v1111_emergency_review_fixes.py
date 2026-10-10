"""Raw ASGI regressions for the emergency release review findings."""

from unittest.mock import patch
import socket

from fastapi.testclient import TestClient
import pytest
import x402.http.middleware.fastapi as official_http
import x402.mcp as official_mcp
import x402.server as official_server

from smartfetch import server
from smartfetch.providers.exa import ExaSearchProvider
from smartfetch.providers.gemini import GeminiProvider
from smartfetch.server import create_app


INVALID_REQUEST = b'{"success":false,"error_code":"invalid_request","error":"Invalid request"}'
INVALID_JSON = b'{"success":false,"error_code":"invalid_json","error":"Invalid JSON"}'
SERVICE_UNHEALTHY = b'{"success":false,"error_code":"service_unhealthy","error":"Service temporarily unavailable"}'


@pytest.fixture(autouse=True)
def forbid_retrieval_and_provider_entry():
    with (
        patch.object(server, "_run_fetch", side_effect=AssertionError("retrieval entered"), create=True) as retrieve,
        patch.object(ExaSearchProvider, "__init__", side_effect=AssertionError("Exa entered")) as exa,
        patch.object(GeminiProvider, "__init__", side_effect=AssertionError("Gemini entered")) as gemini,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        patch.object(socket, "create_connection", side_effect=AssertionError("network entered")) as network,
    ):
        yield
    for boundary in (retrieve, exa, gemini, dns, network):
        boundary.assert_not_called()


def test_escaped_lone_surrogate_mcp_id_is_finite_invalid_request_before_work():
    body = (
        b'{"jsonrpc":"2.0","id":"\\ud800","method":"tools/call",'
        b'"params":{"name":"fetch_webpage","arguments":{"url":"https://example.com/"}}}'
    )
    with (
        patch.object(official_http, "payment_middleware", side_effect=AssertionError("HTTP x402 entered")) as http,
        patch.object(official_mcp, "create_payment_wrapper", side_effect=AssertionError("MCP x402 entered")) as mcp,
        patch.object(official_server.x402ResourceServer, "verify_payment", side_effect=AssertionError("payment verified")) as verify,
        patch.object(official_server.x402ResourceServer, "settle_payment", side_effect=AssertionError("payment settled")) as settle,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        TestClient(create_app(), raise_server_exceptions=False) as client,
    ):
        response = client.post(
            "/mcp", content=body,
            headers={"Content-Type": "application/json", "PAYMENT-SIGNATURE": "private-canary"},
        )
    assert response.status_code == 400
    assert response.content == INVALID_REQUEST
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert b"private-canary" not in response.content
    assert all("payment" not in name.lower() for name in response.headers)
    for boundary in (http, mcp, verify, settle, dns):
        boundary.assert_not_called()


@pytest.mark.parametrize("path", ["/fetch", "/mcp"])
def test_terminal_unhealthy_malformed_json_keeps_validation_precedence(path):
    app = create_app()
    app.app.app.state.terminal_unhealthy = True
    with (
        patch.object(official_http, "payment_middleware", side_effect=AssertionError("HTTP x402 entered")) as http,
        patch.object(official_mcp, "create_payment_wrapper", side_effect=AssertionError("MCP x402 entered")) as mcp,
        patch.object(official_server.x402ResourceServer, "verify_payment", side_effect=AssertionError("payment verified")) as verify,
        patch.object(official_server.x402ResourceServer, "settle_payment", side_effect=AssertionError("payment settled")) as settle,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.post(path, content=b"{bad", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.content == INVALID_JSON
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    for boundary in (http, mcp, verify, settle, dns):
        boundary.assert_not_called()


@pytest.mark.parametrize("path", ["/fetch", "/mcp"])
def test_terminal_unhealthy_valid_retrieval_stays_finite_503(path):
    body = (
        b'{"url":"https://example.com/"}' if path == "/fetch" else
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"fetch_webpage","arguments":{"url":"https://example.com/"}}}'
    )
    app = create_app()
    app.app.app.state.terminal_unhealthy = True
    with (
        patch.object(official_http, "payment_middleware", side_effect=AssertionError("HTTP x402 entered")) as http,
        patch.object(official_mcp, "create_payment_wrapper", side_effect=AssertionError("MCP x402 entered")) as mcp,
        patch.object(official_server.x402ResourceServer, "verify_payment", side_effect=AssertionError("payment verified")) as verify,
        patch.object(official_server.x402ResourceServer, "settle_payment", side_effect=AssertionError("payment settled")) as settle,
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.post(path, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 503
    assert response.content == SERVICE_UNHEALTHY
    assert response.headers["retry-after"] == "3600"
    assert response.headers["cache-control"] == "no-store"
    for boundary in (http, mcp, verify, settle):
        boundary.assert_not_called()


@pytest.mark.parametrize("path", ["/fetch", "/mcp"])
def test_5000_digit_integer_is_finite_invalid_json_before_work(path):
    huge_number = b"1" * 5000
    if path == "/fetch":
        body = b'{"url":"https://example.com/","max_chars":' + huge_number + b"}"
    else:
        body = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"fetch_webpage","arguments":'
            b'{"url":"https://example.com/","max_chars":' + huge_number + b"}}}"
        )
    assert len(body) < 20_000
    with (
        patch.object(official_http, "payment_middleware", side_effect=AssertionError("HTTP x402 entered")) as http,
        patch.object(official_mcp, "create_payment_wrapper", side_effect=AssertionError("MCP x402 entered")) as mcp,
        patch.object(official_server.x402ResourceServer, "verify_payment", side_effect=AssertionError("payment verified")) as verify,
        patch.object(official_server.x402ResourceServer, "settle_payment", side_effect=AssertionError("payment settled")) as settle,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        TestClient(create_app(), raise_server_exceptions=False) as client,
    ):
        response = client.post(path, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.content == INVALID_JSON
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    for boundary in (http, mcp, verify, settle, dns):
        boundary.assert_not_called()


@pytest.mark.parametrize("path", ["/fetch", "/mcp"])
@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
@pytest.mark.parametrize("nested", [False, True])
def test_nonstandard_json_constant_is_finite_invalid_json_before_work(path, constant, nested):
    extra = b'{"value":[' + constant + b"]}" if nested else constant
    if path == "/fetch":
        body = b'{"url":"https://example.com/","extra":' + extra + b"}"
    else:
        body = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"fetch_webpage","arguments":'
            b'{"url":"https://example.com/","extra":' + extra + b"}}}"
        )
    with (
        patch.object(official_http, "payment_middleware", side_effect=AssertionError("HTTP x402 entered")) as http,
        patch.object(official_mcp, "create_payment_wrapper", side_effect=AssertionError("MCP x402 entered")) as mcp,
        patch.object(official_server.x402ResourceServer, "verify_payment", side_effect=AssertionError("payment verified")) as verify,
        patch.object(official_server.x402ResourceServer, "settle_payment", side_effect=AssertionError("payment settled")) as settle,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        TestClient(create_app(), raise_server_exceptions=False) as client,
    ):
        response = client.post(
            path,
            content=body,
            headers={"Content-Type": "application/json", "PAYMENT-SIGNATURE": "private-canary"},
        )
    assert response.status_code == 400
    assert response.content == INVALID_JSON
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert b"private-canary" not in response.content
    assert all("payment" not in name.lower() for name in response.headers)
    for boundary in (http, mcp, verify, settle, dns):
        boundary.assert_not_called()
