"""Comprehensive test suite for SmartFetch production remediation:
- x402 payment enforcement across all 4 MCP tools (unpaid calls rejected with challenge)
- REST POST /fetch payment enforcement (unpaid returns HTTP 402 with PAYMENT-REQUIRED)
- Free discovery (health, meta, openapi, llms, .well-known/x402, .well-known/mcp.json)
- Accurate operational metadata (retrieval_available=True, isolated_browser mode)
"""

import json
import os
import pytest
from starlette.testclient import TestClient

from smartfetch.mcp_server import create_smartfetch_mcp
from smartfetch.payments import load_x402_settings
from smartfetch.server import create_app


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PRICE", "$0.005")
    monkeypatch.setenv("X402_NETWORK", "eip155:8453")
    monkeypatch.setenv("X402_PAY_TO", "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786")
    monkeypatch.setenv("X402_ALLOW_MOCK_FACILITATOR", "true")
    monkeypatch.setenv("BROWSER_WORKER_URL", "https://159.89.40.174:8443")
    monkeypatch.setenv("BROWSER_WORKER_HMAC_KEY", "test-key-mock-for-local-testing-1234567890")
    monkeypatch.setenv("BROWSER_WORKER_SSL_CA", "")


def test_health_reports_isolated_browser(monkeypatch):
    app = create_app()
    with TestClient(app) as client:
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["retrieval_available"] is True
        assert data["browser_available"] is True
        assert data["mode"] == "isolated_browser"


def test_discovery_endpoints(monkeypatch):
    app = create_app()
    with TestClient(app) as client:
        # 1. .well-known/x402
        res = client.get("/.well-known/x402")
        assert res.status_code == 200
        x402_data = res.json()
        assert x402_data["payment"]["network"] == "eip155:8453"
        assert x402_data["payment"]["price"] == "$0.005"
        assert x402_data["payment"]["pay_to"] == "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786"
        assert x402_data["payment"]["enabled"] is True

        # 2. .well-known/mcp.json
        res = client.get("/.well-known/mcp.json")
        assert res.status_code == 200
        mcp_meta = res.json()
        assert len(mcp_meta["tools"]) == 5

        # 3. /meta
        res = client.get("/meta")
        assert res.status_code == 200
        meta_data = res.json()
        assert meta_data["mode"] == "isolated_browser"
        assert meta_data["retrieval_available"] is True

        # 4. /llms.txt
        res = client.get("/llms.txt")
        assert res.status_code == 200
        assert "SmartFetch" in res.text


def test_rest_fetch_unpaid_rejected():
    app = create_app()
    with TestClient(app) as client:
        res = client.post("/fetch", json={"url": "https://example.com"})
        assert res.status_code == 402
        assert "PAYMENT-REQUIRED" in res.headers or "payment-required" in res.headers


def test_mcp_free_methods_and_paid_tools():
    app = create_app()
    with TestClient(app) as client:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        # 1. initialize is free
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        res = client.post("/mcp", json=init_req, headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert "result" in data
        assert data["result"]["serverInfo"]["name"] == "SmartFetch"

        # 2. tools/list is free and lists 4 tools with payment_challenge_enabled
        tools_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        res = client.post("/mcp", json=tools_req, headers=headers)
        assert res.status_code == 200
        data = res.json()
        tools = data["result"]["tools"]
        assert len(tools) == 5
        tool_names = [t["name"] for t in tools]
        for expected in ("fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage", "screenshot_webpage"):
            assert expected in tool_names
        print("FIRST TOOL IN TOOLS LIST:", repr(tools[0]))
        for t in tools:
            meta = t.get("_meta") or t.get("meta") or {}
            assert meta.get("payment_challenge_enabled") is True
            assert meta.get("availability") == "available"

        # 3. Unpaid tool calls for ALL 5 tools MUST be challenged / rejected
        for tool_name in ("fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage", "screenshot_webpage"):
            call_req = {
                "jsonrpc": "2.0",
                "id": f"call-{tool_name}",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {"url": "https://example.com"},
                },
            }
            res = client.post("/mcp", json=call_req, headers=headers)
            assert res.status_code == 200
            data = res.json()
            assert "result" in data
            result = data["result"]
            assert result.get("isError") is True, f"Tool {tool_name} should fail without payment"
            # Structured error content or text challenge
            content_text = result["content"][0]["text"]
            assert "x402" in content_text.lower() or "payment" in content_text.lower() or "402" in content_text or "accepts" in content_text.lower()
