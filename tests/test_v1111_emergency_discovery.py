"""Free emergency discovery cannot advertise a payable retrieval resource."""

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from smartfetch import server
from smartfetch.mcp_server import MCP_TOOLS


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
FREE_PATHS = (
    "/", "/meta", "/docs", "/openapi.json", "/llms.txt",
    "/.well-known/x402", "/.well-known/glama.json", "/robots.txt", "/sitemap.xml",
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


def test_llms_preserves_free_openapi_navigation_with_emergency_status():
    with TestClient(server.create_app(), base_url="https://agent.example") as client:
        response = client.get("/llms.txt")
    assert response.status_code == 200
    assert "- [OpenAPI 3.1](https://agent.example/openapi.json)" in response.text
    assert "mode: emergency_fail_closed" in response.text
    assert "fetch.availability: unavailable" in response.text
    assert "fetch.payment_challenge_enabled: false" in response.text
    assert "HTTP buyer example" not in response.text
    assert "PAYMENT-SIGNATURE" not in response.text


def test_meta_preserves_free_mcp_url_with_emergency_status():
    with TestClient(server.create_app(), base_url="https://agent.example") as client:
        response = client.get("/meta")
    assert response.status_code == 200
    meta = response.json()
    assert meta["mcp"]["url"] == "https://agent.example/mcp"
    assert meta["mode"] == "emergency_fail_closed"
    assert meta["retrieval_available"] is False
    assert meta["fetch"]["availability"] == "unavailable"
    assert meta["fetch"]["payment_challenge_enabled"] is False
    assert all(tool["availability"] == "unavailable" for tool in meta["tools"])
    _assert_no_payable_fields(meta)


def test_meta_preserves_free_discovery_navigation_without_payable_fields():
    base = "https://agent.example"
    with TestClient(server.create_app(), base_url=base) as client:
        meta = client.get("/meta").json()
    assert meta["discovery"] == {
        "x402": f"{base}/.well-known/x402",
        "docs": f"{base}/docs",
        "openapi": f"{base}/openapi.json",
        "llms": f"{base}/llms.txt",
        "robots": f"{base}/robots.txt",
        "sitemap": f"{base}/sitemap.xml",
    }
    assert meta["retrieval_available"] is False
    assert meta["fetch"]["payment_challenge_enabled"] is False
    _assert_no_payable_fields(meta)


def test_root_and_docs_preserve_free_navigation_with_unavailable_status():
    base = "https://agent.example"
    with TestClient(server.create_app(), base_url=base) as client:
        pages = (client.get("/"), client.get("/docs"))
    for response in pages:
        assert response.status_code == 200
        for path in ("/meta", "/.well-known/x402", "/openapi.json", "/llms.txt", "/mcp"):
            assert f'href="{base}{path}"' in response.text
        assert '"retrieval_available":false' in response.text
        assert '"payment_challenge_enabled":false' in response.text
        assert "PAYMENT-REQUIRED" not in response.text
        assert "Paid HTTP retrieval" not in response.text


def test_openapi_preserves_free_server_and_docs_links_without_402():
    base = "https://agent.example"
    with TestClient(server.create_app(), base_url=base) as client:
        document = client.get("/openapi.json").json()
    assert document["servers"] == [{"url": base}]
    assert document["externalDocs"]["url"] == f"{base}/docs"
    assert document["paths"]["/fetch"]["post"]["x-smartfetch-availability"] == "unavailable"
    assert "402" not in document["paths"]["/fetch"]["post"]["responses"]
    _assert_no_payable_fields(document)


def test_llms_preserves_other_free_navigation_without_buyer_links():
    base = "https://agent.example"
    with TestClient(server.create_app(), base_url=base) as client:
        text = client.get("/llms.txt").text
    for label, path in (
        ("Community x402 manifest", "/.well-known/x402"),
        ("Documentation", "/docs"),
        ("Metadata", "/meta"),
        ("Remote MCP Streamable HTTP", "/mcp"),
    ):
        assert f"- [{label}]({base}{path})" in text
    assert "mode: emergency_fail_closed" in text
    assert "fetch.payment_challenge_enabled: false" in text
    assert "HTTP buyer example" not in text
    assert "PAYMENT-SIGNATURE" not in text


def test_x402_manifest_preserves_free_navigation_with_empty_resources():
    base = "https://agent.example"
    with TestClient(server.create_app(), base_url=base) as client:
        manifest = client.get("/.well-known/x402").json()
    assert manifest["homepage"] == base
    assert manifest["endpoints"] == {
        "mcp": {"url": f"{base}/mcp", "transport": "streamable-http"},
        "openapi": f"{base}/openapi.json",
        "llms": f"{base}/llms.txt",
        "docs": f"{base}/docs",
        "metadata": f"{base}/meta",
    }
    assert manifest["resources"] == []
    assert manifest["retrieval_available"] is False
    _assert_no_payable_fields(manifest)


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


def test_sitemap_preserves_the_established_xml_media_type():
    with TestClient(server.create_app()) as client:
        response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert b"<loc>" in response.content
    assert b"/fetch" not in response.content
    assert "payment-required" not in response.headers


def test_glama_ownership_retains_main_contract_without_payment_or_work():
    expected = {
        "$schema": "https://glama.ai/mcp/schemas/connector.json",
        "maintainers": [{"email": "smartfetch.contact@gmail.com"}],
    }
    with patch("x402.http.HTTPFacilitatorClient.get_supported", side_effect=AssertionError("payment entry")):
        with TestClient(server.create_app()) as client:
            response = client.get("/.well-known/glama.json", headers={"PAYMENT-SIGNATURE": "ignored"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == expected
    assert "payment-required" not in response.headers
    assert "payment-response" not in response.headers


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

    document = emergency_openapi_document({
        "base": "http://testserver",
        "fetch": "http://testserver/fetch",
        "docs": "http://testserver/docs",
    })
    _assert_no_payable_fields(document)
    operation = document["paths"]["/fetch"]["post"]
    _assert_unavailable_identity({
        "availability": operation["x-smartfetch-availability"],
        "payment_challenge_enabled": operation["payment_challenge_enabled"],
        **({"normal_price": operation["normal_price"]} if "normal_price" in operation else {}),
    })
