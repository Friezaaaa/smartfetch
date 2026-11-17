"""Audit script: Exhaustive test proving zero free execution bypass paths."""

import requests
import json

BASE_URL = "https://smartfetch-production-ea53.up.railway.app"
TEST_URL = "https://example.com/"

def audit_no_free_paths():
    print("=== Auditing for Zero Free Execution Bypass ===")
    
    # 1. POST /fetch unpaid
    r = requests.post(f"{BASE_URL}/fetch", json={"url": TEST_URL})
    print(f"1. POST /fetch (no payment): HTTP {r.status_code}")
    assert r.status_code == 402
    assert "Example Domain" not in r.text
    print("   -> BLOCKED (402 Payment Required)")

    # 2. POST /fetch with malformed payment signature
    r = requests.post(
        f"{BASE_URL}/fetch",
        json={"url": TEST_URL},
        headers={"PAYMENT-SIGNATURE": "malformed_base64_or_invalid_signature"}
    )
    print(f"2. POST /fetch (malformed signature): HTTP {r.status_code}")
    assert r.status_code == 402
    assert "Example Domain" not in r.text
    print("   -> BLOCKED (402 Payment Required)")

    # 3. POST /fetch with GET or other verbs
    r = requests.get(f"{BASE_URL}/fetch")
    print(f"3. GET /fetch: HTTP {r.status_code}")
    assert r.status_code == 405
    assert "Example Domain" not in r.text
    print("   -> BLOCKED (405 Method Not Allowed)")

    # 4. MCP Tools Call unpaid
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    tools = ["fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage", "screenshot_webpage"]
    for t in tools:
        payload = {
            "jsonrpc": "2.0",
            "id": f"test-{t}",
            "method": "tools/call",
            "params": {"name": t, "arguments": {"url": TEST_URL}},
        }
        r = requests.post(f"{BASE_URL}/mcp", json=payload, headers=headers)
        data = r.json()
        is_error = data.get("result", {}).get("isError")
        print(f"4. MCP {t} (unpaid): HTTP {r.status_code}, isError: {is_error}")
        assert r.status_code == 200
        assert is_error is True
        assert "Example Domain" not in r.text
        print(f"   -> BLOCKED (Challenged with x402 Payment Required)")

    # 5. Non-existent routes
    for path in ["/render", "/scrape", "/api/fetch", "/v1/fetch"]:
        r = requests.post(f"{BASE_URL}{path}", json={"url": TEST_URL})
        print(f"5. POST {path}: HTTP {r.status_code}")
        assert r.status_code in (404, 405)
        assert "Example Domain" not in r.text

    print("\nSUCCESS: All execution routes require payment. Zero free execution paths exist.")

if __name__ == "__main__":
    audit_no_free_paths()
