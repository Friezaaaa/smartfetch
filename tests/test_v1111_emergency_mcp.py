"""Emergency MCP transport: free discovery, no executable retrieval."""

import asyncio
import json
import re
import socket
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
import x402.mcp as official_mcp
from x402.server import x402ResourceServer

from smartfetch import server
from smartfetch.config import SERVICE_VERSION
from smartfetch.mcp_server import MCP_TOOLS, create_smartfetch_mcp


HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ORDINARY = {
    "success": False,
    "error_code": "retrieval_unavailable",
    "error": "Retrieval is temporarily unavailable",
}
BROWSER = {
    "success": False,
    "error_code": "browser_unavailable",
    "error": "Browser rendering is temporarily unavailable",
}


def assert_shared_headers(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert re.fullmatch(r"[0-9a-f]{16}", response.headers["x-request-id"])
    for name in (b"cache-control", b"x-content-type-options", b"x-request-id"):
        assert sum(key.lower() == name for key, _ in response.headers.raw) == 1


def rpc(client, method, params=None, request_id=1, headers=None):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return client.post("/mcp", json=payload, headers={**HEADERS, **(headers or {})})


def initialize(client):
    return rpc(client, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "emergency-test", "version": "1"},
    })


def expected_call_result(error):
    return {
        "content": [{"type": "text", "text": error["error"]}],
        "structuredContent": error,
        "isError": True,
    }


def test_initialize_and_list_are_free_and_four_tools_are_natively_unavailable():
    with TestClient(server.create_app()) as client:
        started = initialize(client)
        listed = rpc(client, "tools/list", {}, 2)
    assert started.status_code == listed.status_code == 200
    assert_shared_headers(started)
    assert_shared_headers(listed)
    assert started.json()["result"]["serverInfo"]["name"] == "SmartFetch"
    assert started.json()["result"]["serverInfo"]["version"] == SERVICE_VERSION
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == list(MCP_TOOLS)
    for tool in tools:
        assert tool["_meta"]["availability"] == "unavailable"
        assert tool["_meta"]["payment_challenge_enabled"] is False
        assert "accepts" not in json.dumps(tool)
        assert "payment-required" not in json.dumps(tool).lower()
        assert "payment-requirements" not in json.dumps(tool).lower()
    expected_common = {
        "url": {"title": "Url", "type": "string"},
        "max_chars": {"default": 20000, "maximum": 50000, "minimum": 1000,
                      "title": "Max Chars", "type": "integer"},
    }
    for tool in tools[:3]:
        assert tool["inputSchema"]["properties"] == {
            **expected_common,
            "force_browser": {"default": False, "title": "Force Browser", "type": "boolean"},
        }
        assert tool["inputSchema"]["required"] == ["url"]
    assert tools[3]["inputSchema"]["properties"] == expected_common
    assert tools[3]["inputSchema"]["required"] == ["url"]


@pytest.mark.parametrize(("tool", "browser"), [
    ("fetch_webpage", False),
    ("fetch_webpage", True),
    ("webpage_to_markdown", False),
    ("webpage_to_markdown", True),
    ("extract_webpage_text", False),
    ("extract_webpage_text", True),
    ("render_webpage", True),
])
def test_every_tool_call_is_exact_bounded_unavailable_before_sdk_handler(tool, browser):
    app = server.create_app()
    args = {"url": "https://example.com/private-canary", "max_chars": 20000}
    if tool != "render_webpage":
        args["force_browser"] = browser
    with TestClient(app) as client:
        result = rpc(client, "tools/call", {"name": tool, "arguments": args}, 1)
    expected = BROWSER if browser else ORDINARY
    body = {"jsonrpc": "2.0", "id": 1, "result": expected_call_result(expected)}
    encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    assert result.status_code == 200
    assert_shared_headers(result)
    assert result.content == encoded
    assert result.headers["content-type"] == "application/json; charset=utf-8"
    assert result.headers["cache-control"] == "no-store"
    assert len(result.content) <= (387 if browser else 373)
    assert "payment-response" not in result.headers
    assert "payment-required" not in result.headers
    assert b"private-canary" not in result.content


@pytest.mark.parametrize("method", ["GET", "DELETE", "OPTIONS", "HEAD", "PUT"])
def test_non_post_mcp_methods_are_405_before_sdk(method):
    with TestClient(server.create_app()) as client:
        result = client.request(method, "/mcp", headers=HEADERS)
    assert result.status_code == 405
    assert_shared_headers(result)
    assert result.headers["allow"] == "POST"
    assert result.headers["cache-control"] == "no-store"
    assert result.content == (b"" if method == "HEAD" else b'{"success":false,"error_code":"method_not_allowed","error":"Method not allowed"}')


@pytest.mark.parametrize("method", [
    "resources/list", "resources/read", "resources/subscribe", "resources/unsubscribe",
    "prompts/list", "prompts/get", "completion/complete", "logging/setLevel",
    "notifications/resources/updated", "subscriptions/list", "future/unknown",
])
def test_other_mcp_methods_are_finite_not_available(method):
    with TestClient(server.create_app()) as client:
        result = rpc(client, method, {}, 7)
    assert result.status_code == 200
    assert_shared_headers(result)
    assert result.json() == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "Method not available"}}
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("name", ["search_and_extract", "extract_structured_data", "future_tool"])
def test_unknown_tools_are_not_executable(name):
    with TestClient(server.create_app()) as client:
        result = rpc(client, "tools/call", {"name": name, "arguments": {}}, 8)
    assert result.status_code == 200
    assert result.json() == {"jsonrpc": "2.0", "id": 8, "error": {"code": -32602, "message": "Unknown tool"}}


@pytest.mark.parametrize("body", [
    b"[{}]", b"{", b'{"jsonrpc":"2.0","method":"tools/list"}',
    b'{"jsonrpc":"2.0","id":1,"method":"notifications/initialized"}',
])
def test_malformed_mcp_envelopes_are_not_unavailable(body):
    with TestClient(server.create_app()) as client:
        result = client.post("/mcp", content=body, headers=HEADERS)
    assert result.status_code == 400
    assert_shared_headers(result)
    assert b"unavailable" not in result.content
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(("method", "params", "request_id"), [
    ("initialize", {"clientInfo": "private-canary"}, 1),
    ("ping", "private-canary", 2),
    ("tools/list", {"cursor": {"private": "private-canary"}}, 3),
    ("notifications/initialized", "private-canary", None),
])
def test_malformed_free_method_params_are_private_and_rejected_before_sdk_or_work(
    method, params, request_id, caplog,
):
    payload = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        payload["id"] = request_id
    with (
        patch.object(official_mcp, "create_payment_wrapper", side_effect=AssertionError("payment entered")) as payment,
        patch.object(x402ResourceServer, "verify_payment", side_effect=AssertionError("verification entered")) as verify,
        patch.object(x402ResourceServer, "settle_payment", side_effect=AssertionError("settlement entered")) as settle,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        TestClient(server.create_app()) as client,
    ):
        result = client.post("/mcp", json=payload, headers=HEADERS)
    assert result.status_code == 400
    assert_shared_headers(result)
    assert result.content == b'{"success":false,"error_code":"invalid_request","error":"Invalid request"}'
    assert len(result.content) <= 128
    assert "private-canary" not in caplog.text
    assert "private-canary" not in result.text
    assert "payment-response" not in result.headers
    payment.assert_not_called()
    verify.assert_not_called()
    settle.assert_not_called()
    dns.assert_not_called()


@pytest.mark.parametrize("method", [
    "initialize", "ping", "tools/list", "notifications/initialized",
])
def test_deep_extra_free_method_params_fail_before_sdk_serialization_or_logging(method, caplog):
    nested = {"private": "private-review-canary"}
    for _ in range(300):
        nested = {"next": nested}
    params = {"extra": nested}
    if method == "initialize":
        params.update({
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "emergency-test", "version": "1"},
        })
    payload = {"jsonrpc": "2.0", "method": method, "params": params}
    if method != "notifications/initialized":
        payload["id"] = 9
    with (
        patch.object(official_mcp, "create_payment_wrapper", side_effect=AssertionError("payment entered")) as payment,
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS entered")) as dns,
        TestClient(server.create_app()) as client,
    ):
        result = client.post("/mcp", content=json.dumps(payload).encode(), headers=HEADERS)
    assert result.status_code == 400
    assert_shared_headers(result)
    assert result.content == b'{"success":false,"error_code":"invalid_request","error":"Invalid request"}'
    assert "private-review-canary" not in result.text
    assert "private-review-canary" not in caplog.text
    payment.assert_not_called()
    dns.assert_not_called()


def test_payment_headers_are_opaque_and_repeated_requests_remain_unavailable():
    with TestClient(server.create_app()) as client:
        results = [
            rpc(client, "tools/call", {"name": "fetch_webpage", "arguments": {"url": "https://example.com/"}}, i,
                {"PAYMENT-SIGNATURE": "payment-canary"})
            for i in range(1, 4)
        ]
    assert all(result.status_code == 200 for result in results)
    assert all(result.json()["result"]["isError"] for result in results)
    assert all(b"payment-canary" not in result.content for result in results)


def test_public_calls_never_enter_sdk_tool_dispatch_even_with_duplicate_payment_headers():
    app = server.create_app()
    sdk = app.app.app.state.smartfetch_mcp.fastmcp
    with patch.object(sdk, "call_tool", AsyncMock(side_effect=AssertionError("SDK handler entered"))) as handler:
        with TestClient(app) as client:
            result = client.post(
                "/mcp",
                content=b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"fetch_webpage","arguments":{"url":"https://example.com/"}}}',
                headers=[
                    ("Content-Type", "application/json"),
                    ("Accept", "application/json, text/event-stream"),
                    ("PAYMENT-SIGNATURE", "first-canary"),
                    ("payment-signature", "second-canary"),
                ],
            )
    assert result.status_code == 200
    assert result.json()["result"] == expected_call_result(ORDINARY)
    handler.assert_not_awaited()
    assert b"canary" not in result.content


@pytest.mark.parametrize("tool", MCP_TOOLS)
def test_unwrapped_static_handler_itself_fails_closed(tool):
    emergency = create_smartfetch_mcp(object(), AsyncMock(side_effect=AssertionError("retrieval entered")))
    registered = next(item for item in emergency.fastmcp._tool_manager.list_tools() if item.name == tool)
    result = asyncio.run(registered.run({"url": "https://example.com/"}, convert_result=False))
    assert result.isError is True
    assert result.structuredContent == (BROWSER if tool == "render_webpage" else ORDINARY)


def test_direct_factory_with_enabled_settings_never_constructs_paid_wrapper():
    with patch("x402.mcp.create_payment_wrapper", side_effect=AssertionError("paid wrapper entered")) as paid:
        result = create_smartfetch_mcp(object(), AsyncMock(side_effect=AssertionError("handler entered")))
    assert result.resource_server is None
    assert result.accepts == []
    paid.assert_not_called()
