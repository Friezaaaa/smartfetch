"""Audit script: Detailed verification of all 5 production discovery documents."""

import requests
import json

BASE_URL = "https://smartfetch-production-ea53.up.railway.app"

def audit_discovery():
    print("=== Auditing Discovery Endpoints Contract ===")

    # 1. /.well-known/x402
    r_x402 = requests.get(f"{BASE_URL}/.well-known/x402", timeout=10)
    print(f"1. GET /.well-known/x402: HTTP {r_x402.status_code}")
    assert r_x402.status_code == 200
    x402_data = r_x402.json()
    assert x402_data["spec"] == "agent402-service-manifest/1"
    assert x402_data["name"] == "SmartFetch"
    payment = x402_data["payment"]
    assert payment["protocol"] == "x402"
    assert payment["x402Version"] == 2
    assert payment["enabled"] is True
    assert payment["network"] == "eip155:8453"
    assert payment["price"] == "$0.005"
    assert payment["asset"] == "USDC"
    assert payment["payTo"] == "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786"
    assert "emergency" not in json.dumps(x402_data).lower()
    print("   -> PASS: /.well-known/x402 matches authoritative Base Mainnet USDC spec")

    # 2. /.well-known/mcp.json
    r_mcp = requests.get(f"{BASE_URL}/.well-known/mcp.json", timeout=10)
    print(f"2. GET /.well-known/mcp.json: HTTP {r_mcp.status_code}")
    assert r_mcp.status_code == 200
    mcp_data = r_mcp.json()
    assert mcp_data["name"] == "SmartFetch"
    assert mcp_data["version"] in ("1.12.0", "1.13.0", "1.13.1")
    assert mcp_data["transport"] == "streamable-http"
    tools = [t["name"] for t in mcp_data["tools"]]
    assert tools == ["fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage"]
    print("   -> PASS: /.well-known/mcp.json correctly lists 4 production tools")

    # 3. /meta
    r_meta = requests.get(f"{BASE_URL}/meta", timeout=10)
    print(f"3. GET /meta: HTTP {r_meta.status_code}")
    assert r_meta.status_code == 200
    meta_data = r_meta.json()
    assert meta_data["service"] == "SmartFetch"
    assert meta_data["version"] in ("1.12.0", "1.13.0", "1.13.1")
    assert meta_data["mode"] == "isolated_browser"
    assert meta_data["retrieval_available"] is True
    assert meta_data["browser_available"] is True
    assert meta_data["fetch"]["availability"] == "available"
    assert meta_data["fetch"]["payment_challenge_enabled"] is True
    print("   -> PASS: /meta reflects isolated_browser and available state")

    # 4. /openapi.json
    r_openapi = requests.get(f"{BASE_URL}/openapi.json", timeout=10)
    print(f"4. GET /openapi.json: HTTP {r_openapi.status_code}")
    assert r_openapi.status_code == 200
    openapi = r_openapi.json()
    assert "openapi" in openapi
    assert "/fetch" in openapi["paths"]
    assert "post" in openapi["paths"]["/fetch"]
    print("   -> PASS: /openapi.json valid and documents POST /fetch")

    # 5. /llms.txt
    r_llms = requests.get(f"{BASE_URL}/llms.txt", timeout=10)
    print(f"5. GET /llms.txt: HTTP {r_llms.status_code}")
    assert r_llms.status_code == 200
    assert "SmartFetch" in r_llms.text
    assert "/mcp" in r_llms.text
    assert "/fetch" in r_llms.text
    assert "emergency" not in r_llms.text.lower()
    print("   -> PASS: /llms.txt present and reflects active service")

    print("\nSUCCESS: All 5 discovery endpoints match production contracts perfectly.")

if __name__ == "__main__":
    audit_discovery()
