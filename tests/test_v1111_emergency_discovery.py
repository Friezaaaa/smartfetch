"""Free emergency discovery cannot advertise a payable retrieval resource."""

import json

from fastapi.testclient import TestClient

from smartfetch import server
from smartfetch.mcp_server import MCP_TOOLS


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
FREE_PATHS = (
    "/", "/meta", "/docs", "/openapi.json", "/llms.txt",
    "/.well-known/x402", "/robots.txt", "/sitemap.xml",
)
FORBIDDEN_KEYS = {
    "accepts", "paymentrequirements", "scheme", "network", "asset", "payto",
    "payment-required", "payment-response", "x-payment", "x402/bazaar",
}


def _assert_no_payable_fields(value):
    if isinstance(value, dict):
        for key, child in value.items():
            assert key.lower() not in FORBIDDEN_KEYS
            _assert_no_payable_fields(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_payable_fields(child)


def _assert_unavailable_identity(record):
    assert record["availability"] == "unavailable"
    assert record["payment_challenge_enabled"] is False
    if "normal_price" in record:
        assert isinstance(record["normal_price"], str)


def test_every_free_discovery_route_is_available_without_payment():
    with TestClient(server.create_app()) as client:
        results = {path: client.get(path) for path in FREE_PATHS}
    for path, response in results.items():
        assert response.status_code == 200, path
        assert "payment-required" not in response.headers
        assert "payment-response" not in response.headers
        assert b"PaymentRequirements" not in response.content
        assert b'"accepts"' not in response.content


def test_root_meta_and_x402_manifest_are_machine_readable_but_not_payable():
    with TestClient(server.create_app()) as client:
        root = client.get("/")
        meta = client.get("/meta").json()
        manifest = client.get("/.well-known/x402").json()
    assert b'id="smartfetch-availability"' in root.content
    assert b'"availability":"unavailable"' in root.content
    assert b'"payment_challenge_enabled":false' in root.content
    assert meta["mode"] == "emergency_fail_closed"
    assert meta["retrieval_available"] is False
    _assert_unavailable_identity(meta["fetch"])
    assert [item["name"] for item in meta["tools"]] == list(MCP_TOOLS)
    for item in meta["tools"]:
        _assert_unavailable_identity(item)
    assert manifest["resources"] == []
    assert manifest["retrieval_available"] is False
    _assert_no_payable_fields(meta)
    _assert_no_payable_fields(manifest)


def test_openapi_docs_and_llms_report_unavailable_not_success_or_402():
    with TestClient(server.create_app()) as client:
        openapi = client.get("/openapi.json").json()
        docs = client.get("/docs").text
        llms = client.get("/llms.txt").text
    operation = openapi["paths"]["/fetch"]["post"]
    assert operation["x-smartfetch-availability"] == "unavailable"
    assert operation["payment_challenge_enabled"] is False
    assert set(operation["responses"]) == {"503", "400", "415", "422"}
    assert all(path not in openapi["paths"] for path in (
        "/search-and-extract/results", "/search-and-extract/answer",
        "/search-and-extract/structured", "/extract-structured-data/webpage",
        "/extract-structured-data/image", "/extract-structured-data/pdf",
        "/extract-structured-data/audio", "/extract-structured-data/video",
    ))
    assert 'id="smartfetch-availability"' in docs
    assert '"availability":"unavailable"' in docs
    assert "availability: unavailable" in llms
    assert "payment_challenge_enabled: false" in llms
    assert "Payment Required" not in docs
    assert "Payment Required" not in llms
    _assert_no_payable_fields(openapi)


def test_robots_and_sitemap_remain_free_ownership_metadata():
    with TestClient(server.create_app()) as client:
        robots = client.get("/robots.txt")
        sitemap = client.get("/sitemap.xml")
    assert "User-agent: *" in robots.text
    assert "Sitemap:" in robots.text
    assert "/meta" in sitemap.text
    assert "/openapi.json" in sitemap.text
    assert "/fetch" not in sitemap.text
    assert "payment" not in (robots.text + sitemap.text).lower()


def test_mcp_listing_agrees_and_has_no_paid_extension():
    with TestClient(server.create_app()) as client:
        response = client.post(
            "/mcp", headers=MCP_HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == list(MCP_TOOLS)
    for tool in tools:
        _assert_unavailable_identity(tool["_meta"])
        _assert_no_payable_fields(tool)
        assert "temporarily unavailable" in tool["description"].lower()
        assert "payment challenge" not in tool["description"].lower()


def test_historical_normal_price_without_adjacent_unavailable_marker_is_invalid():
    from smartfetch.discovery import emergency_openapi_document

    document = emergency_openapi_document({"base": "http://testserver", "fetch": "http://testserver/fetch"})
    _assert_no_payable_fields(document)
    operation = document["paths"]["/fetch"]["post"]
    _assert_unavailable_identity({
        "availability": operation["x-smartfetch-availability"],
        "payment_challenge_enabled": operation["payment_challenge_enabled"],
        **({"normal_price": operation["normal_price"]} if "normal_price" in operation else {}),
    })
