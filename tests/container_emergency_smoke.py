"""Run inside the built emergency image against its own loopback service."""

import argparse
import importlib.metadata
import json
import os
import shutil
import sys
import urllib.error
import urllib.request


REST_404 = (
    "/search-and-extract/results", "/search-and-extract/answer",
    "/search-and-extract/structured", "/extract-structured-data/webpage",
    "/extract-structured-data/image", "/extract-structured-data/pdf",
    "/extract-structured-data/audio", "/extract-structured-data/video",
)
FREE = (
    "/", "/meta", "/docs", "/openapi.json", "/llms.txt",
    "/.well-known/x402", "/robots.txt", "/sitemap.xml",
)
TOOLS = ("fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage")
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def request(base, path, payload=None, headers=None):
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(base + path, data=body, headers=headers or {}, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, {key.lower(): value for key, value in response.headers.items()}, response.read(32769)
    except urllib.error.HTTPError as response:
        return response.code, {key.lower(): value for key, value in response.headers.items()}, response.read(32769)


def rpc(base, method, params=None, request_id=1):
    return request(base, "/mcp", {
        "jsonrpc": "2.0", "id": request_id, "method": method,
        **({"params": params} if params is not None else {}),
    }, MCP_HEADERS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    base = parser.parse_args().base_url.rstrip("/")
    assert base == "http://127.0.0.1:8787", "only container loopback is allowed"
    assert os.getuid() == os.getgid() == 10001
    assert all(shutil.which(name) is None for name in ("chromium", "ffmpeg", "ffprobe"))
    assert importlib.metadata.version("pypdf") == "6.19.0"
    assert importlib.metadata.version("pip") == "26.2.1"

    status, _, data = request(base, "/health")
    assert status == 200
    assert {key: json.loads(data)[key] for key in ("version", "mode", "retrieval_available", "browser_available")} == {
        "version": "1.11.1", "mode": "emergency_fail_closed",
        "retrieval_available": False, "browser_available": False,
    }
    for path in FREE:
        status, headers, data = request(base, path)
        assert status == 200 and len(data) <= 32768, path
        assert not any("payment" in key.lower() for key in headers), path
    status, _, data = request(base, "/meta")
    meta = json.loads(data)
    assert meta["retrieval_available"] is False
    assert [item["name"] for item in meta["tools"]] == list(TOOLS)
    status, _, data = request(base, "/.well-known/x402")
    assert json.loads(data)["resources"] == []
    status, _, data = request(base, "/openapi.json")
    assert all(path not in json.loads(data)["paths"] for path in REST_404)
    for path in REST_404:
        assert request(base, path, {"url": "https://example.invalid/"})[0] == 404
    for browser, code in ((False, "retrieval_unavailable"), (True, "browser_unavailable")):
        status, headers, data = request(base, "/fetch", {
            "url": "https://example.invalid/", "force_browser": browser,
        }, {"Content-Type": "application/json", "PAYMENT-SIGNATURE": "inert-canary"})
        assert status == 503 and json.loads(data)["error_code"] == code
        assert headers["retry-after"] == "3600" and headers["cache-control"] == "no-store"
        assert not any("payment" in key.lower() for key in headers)

    status, _, data = rpc(base, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "container-smoke", "version": "1"},
    })
    assert status == 200 and json.loads(data)["result"]["serverInfo"]["name"] == "SmartFetch"
    status, _, data = rpc(base, "tools/list", {})
    assert status == 200
    tools = json.loads(data)["result"]["tools"]
    assert [item["name"] for item in tools] == list(TOOLS)
    assert all(item["_meta"]["availability"] == "unavailable" for item in tools)
    for tool in TOOLS:
        status, headers, data = rpc(base, "tools/call", {
            "name": tool, "arguments": {"url": "https://example.invalid/"},
        })
        assert status == 200 and json.loads(data)["result"]["isError"] is True
        assert not any("payment" in key.lower() for key in headers)
    assert all(item["name"] not in ("search_and_extract", "extract_structured_data") for item in tools)
    print("container_emergency_smoke: PASS (8 discovery, 8 hidden REST, 2 fetch, 4 MCP tools)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("container_emergency_smoke: FAIL", file=sys.stderr)
        sys.exit(1)
