"""Production Audit Validation Script for live SmartFetch Railway deployment."""

import json
import requests

BASE_URL = "https://smartfetch-production-ea53.up.railway.app"

def test_audit():
    print(f"=== Auditing Live Production: {BASE_URL} ===")

    # 1. /health
    print("\n--- 1. Testing GET /health ---")
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    print(f"Status: {r.status_code}")
    print(f"Body: {r.text}")
    assert r.status_code == 200
    health = r.json()
    assert health["ok"] is True
    assert health["retrieval_available"] is True
    assert health["browser_available"] is True
    assert health["mode"] == "isolated_browser"
    print("VERIFIED: /health is reporting isolated_browser and operational status.")

    # 2. Discovery Endpoints
    print("\n--- 2. Testing Discovery Endpoints ---")
    
    # 2a. /.well-known/x402
    r_x402 = requests.get(f"{BASE_URL}/.well-known/x402", timeout=10)
    print(f"GET /.well-known/x402: {r_x402.status_code}")
    x402_data = r_x402.json()
    print("x402 manifest payment:", json.dumps(x402_data.get("payment"), indent=2))
    assert r_x402.status_code == 200
    assert x402_data["payment"]["enabled"] is True
    assert x402_data["payment"]["network"] == "eip155:8453"
    assert x402_data["payment"]["price"] == "$0.005"
    assert x402_data["payment"]["payTo"] == "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786"
    assert "emergency" not in json.dumps(x402_data).lower()
    print("VERIFIED: /.well-known/x402 correctly advertises Base Mainnet $0.005 USDC without emergency flags.")

    # 2b. /.well-known/mcp.json
    r_mcp = requests.get(f"{BASE_URL}/.well-known/mcp.json", timeout=10)
    print(f"GET /.well-known/mcp.json: {r_mcp.status_code}")
    print(f"MCP discovery tools: {[t['name'] for t in r_mcp.json().get('tools', [])]}")
    assert r_mcp.status_code == 200
    assert len(r_mcp.json()["tools"]) == 4
    print("VERIFIED: /.well-known/mcp.json is published and accurate.")

    # 2c. /meta
    r_meta = requests.get(f"{BASE_URL}/meta", timeout=10)
    print(f"GET /meta: {r_meta.status_code}")
    meta = r_meta.json()
    print(f"Meta mode: {meta.get('mode')}, retrieval_available: {meta.get('retrieval_available')}")
    assert r_meta.status_code == 200
    assert meta["mode"] == "isolated_browser"
    assert meta["retrieval_available"] is True
    print("VERIFIED: /meta reports isolated_browser mode.")

    # 2d. /openapi.json
    r_openapi = requests.get(f"{BASE_URL}/openapi.json", timeout=10)
    print(f"GET /openapi.json: {r_openapi.status_code}")
    assert r_openapi.status_code == 200
    openapi = r_openapi.json()
    assert "/fetch" in openapi.get("paths", {})
    print("VERIFIED: /openapi.json includes /fetch.")

    # 2e. /llms.txt
    r_llms = requests.get(f"{BASE_URL}/llms.txt", timeout=10)
    print(f"GET /llms.txt: {r_llms.status_code}")
    assert r_llms.status_code == 200
    assert "SmartFetch" in r_llms.text
    print("VERIFIED: /llms.txt is present.")

    # 3. MCP initialize
    print("\n--- 3. Testing MCP initialize (Free) ---")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "production-auditor", "version": "1.0"},
        },
    }
    r_init = requests.post(f"{BASE_URL}/mcp", json=init_payload, headers=headers, timeout=10)
    print(f"MCP initialize status: {r_init.status_code}")
    print(f"MCP initialize body: {r_init.text}")
    assert r_init.status_code == 200
    init_data = r_init.json()
    assert "result" in init_data
    assert init_data["result"]["serverInfo"]["name"] == "SmartFetch"
    print("VERIFIED: MCP initialize succeeded and is free.")

    # 4. MCP tools/list
    print("\n--- 4. Testing MCP tools/list (Free) ---")
    list_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    r_list = requests.post(f"{BASE_URL}/mcp", json=list_payload, headers=headers, timeout=10)
    print(f"MCP tools/list status: {r_list.status_code}")
    list_data = r_list.json()
    tools = list_data["result"]["tools"]
    print(f"Available tools ({len(tools)}):")
    for t in tools:
        meta_info = t.get("_meta") or t.get("meta") or {}
        print(f"  - {t['name']}: {t['description'][:60]}... | meta={meta_info}")
        assert meta_info.get("payment_challenge_enabled") is True
        assert meta_info.get("availability") == "available"
    assert len(tools) == 4
    tool_names = [t["name"] for t in tools]
    for required in ("fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage"):
        assert required in tool_names
    print("VERIFIED: MCP tools/list returned all 4 production tools with payment challenges enabled.")

    # 5. MCP tools/call - Verify Payment Enforcement across ALL 4 Tools
    print("\n--- 5. Testing MCP tools/call Unpaid Payment Challenges ---")
    for tool_name in ("fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage"):
        call_payload = {
            "jsonrpc": "2.0",
            "id": f"call-{tool_name}",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"url": "https://example.com"},
            },
        }
        r_call = requests.post(f"{BASE_URL}/mcp", json=call_payload, headers=headers, timeout=10)
        print(f"\nTool '{tool_name}' Call status: {r_call.status_code}")
        call_res = r_call.json()
        print(f"Response: {json.dumps(call_res, indent=2)}")
        assert r_call.status_code == 200
        result = call_res.get("result", {})
        assert result.get("isError") is True, f"Unpaid tool call for {tool_name} was NOT rejected!"
        text_content = result.get("content", [{}])[0].get("text", "")
        # Confirm payment challenge details
        assert "402" in text_content or "payment" in text_content.lower() or "accepts" in text_content.lower() or "challenge" in text_content.lower()
        print(f"VERIFIED: Unpaid call to {tool_name} was blocked with standard x402 payment challenge.")

    # 6. REST POST /fetch - Verify Payment Enforcement
    print("\n--- 6. Testing REST POST /fetch Unpaid Payment Enforcement ---")
    r_fetch = requests.post(f"{BASE_URL}/fetch", json={"url": "https://example.com"}, timeout=10)
    print(f"REST POST /fetch status: {r_fetch.status_code}")
    print(f"Headers: {dict(r_fetch.headers)}")
    print(f"Body: {r_fetch.text[:200]}")
    assert r_fetch.status_code == 402, f"Expected 402 Payment Required, got {r_fetch.status_code}"
    assert "payment-required" in [k.lower() for k in r_fetch.headers.keys()]
    print("VERIFIED: REST POST /fetch returned HTTP 402 Payment Required with payment-required header.")

    print("\n=============================================")
    print("ALL PRODUCTION AUDIT VERIFICATIONS PASSED 100%!")
    print("=============================================")

if __name__ == "__main__":
    test_audit()
