"""Unit and integration tests for Build 3: Parallel Batch Fetch & Structured Text Docs."""

import json
from unittest.mock import MagicMock, patch
import pytest
from starlette.testclient import TestClient

from smartfetch.batch import BatchEscalationLimiter, batch_fetch_urls
from smartfetch.config import SERVICE_NAME, SERVICE_VERSION
from smartfetch.core import smart_fetch
from smartfetch.diagnostics import RetrievalFailure
from smartfetch.extract import extract_plain_document
from smartfetch.payments import X402Settings
from smartfetch.server import create_app

VALID_ADDRESS = "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786"
BASE_MAINNET = "eip155:8453"

PAID_SETTINGS = X402Settings(
    enabled=True,
    pay_to=VALID_ADDRESS,
    price="$0.005",
    network=BASE_MAINNET,
)
FREE_SETTINGS = X402Settings(
    enabled=False,
    pay_to=None,
    price="$0.005",
    network=BASE_MAINNET,
)


# =====================================================================
# 1. Plain Text Documents (.md, .json, .csv, .xml) in Tier 1
# =====================================================================

def test_extract_plain_document_markdown():
    raw_md = "# Hello World\n\nThis is a clean markdown doc with **bold** text."
    url = "https://example.com/docs/guide.md"
    extracted = extract_plain_document(raw_md, url, "text/markdown")

    assert extracted["title"] == "guide.md"
    assert extracted["content"] == raw_md.strip()
    assert extracted["markdown"] == raw_md.strip()
    assert extracted["links"] == []
    assert extracted["word_count"] > 5
    assert len(extracted["content_hash"]) == 64
    assert extracted["low_quality"] is False


def test_extract_plain_document_json():
    raw_json = '{"name": "SmartFetch", "version": "1.13.1", "active": true}'
    url = "https://example.com/api/data.json"
    extracted = extract_plain_document(raw_json, url, "application/json")

    assert extracted["title"] == "data.json"
    assert "SmartFetch" in extracted["content"]
    assert extracted["markdown"].startswith("```json\n")
    assert extracted["links"] == []
    assert extracted["low_quality"] is False


def test_extract_plain_document_csv():
    raw_csv = "id,name,role\n1,alice,admin\n2,bob,user"
    url = "https://example.com/reports/users.csv"
    extracted = extract_plain_document(raw_csv, url, "text/csv")

    assert extracted["title"] == "users.csv"
    assert extracted["content"] == raw_csv.strip()
    assert extracted["markdown"].startswith("```csv\n")
    assert extracted["links"] == []
    assert extracted["low_quality"] is False


def test_extract_plain_document_xml():
    raw_xml = "<rss><channel><title>News</title><item><title>Item 1</title></item></channel></rss>"
    url = "https://example.com/feed.xml"
    extracted = extract_plain_document(raw_xml, url, "application/rss+xml")

    assert extracted["title"] == "feed.xml"
    assert extracted["markdown"].startswith("```xml\n")
    assert extracted["links"] == []
    assert extracted["low_quality"] is False


def test_smart_fetch_structured_text_never_escalates():
    """Verify smart_fetch extracts structured text in Tier 1 without calling browser_fetch."""
    mock_http_response = {
        "status_code": 200,
        "final_url": "https://example.com/schema.json",
        "html": '{"service": "smartfetch", "version": 1}',
        "content_type": "application/json",
        "headers": {"content-type": "application/json"},
    }

    with patch("smartfetch.core.http_fetch", return_value=mock_http_response) as mock_http, \
         patch("smartfetch.core.browser_fetch") as mock_browser:
        res = smart_fetch("https://example.com/schema.json", no_cache=True)
        assert res["success"] is True
        assert res["render_method"] == "http"
        assert res["retrieval_tier"] == "http"
        assert res["content_type"] == "application/json"
        assert "```json" in res["markdown"]
        mock_http.assert_called_once()
        mock_browser.assert_not_called()


# =====================================================================
# 2. Bounded Parallel Batch Fetcher (smartfetch/batch.py)
# =====================================================================

def test_batch_escalation_limiter():
    limiter = BatchEscalationLimiter(max_escalations=2)
    assert limiter.allow_escalation() is True
    assert limiter.allow_escalation() is True
    assert limiter.allow_escalation() is False
    assert limiter.allow_escalation() is False


def test_batch_fetch_urls_validation():
    # Empty list
    with pytest.raises(ValueError, match="Batch must contain between 1 and 5 unique URLs"):
        batch_fetch_urls([])

    # Over 5 URLs
    with pytest.raises(ValueError, match="Batch cannot exceed 5 unique URLs"):
        batch_fetch_urls([f"https://example.com/{i}" for i in range(6)])

    # SSRF / private IP
    with pytest.raises(ValueError):
        batch_fetch_urls(["http://127.0.0.1:8000/private", "https://example.com/"])


def test_batch_fetch_urls_concurrency_and_order():
    """Verify batch_fetch_urls fetches concurrently and preserves order."""
    def mock_fetch(url, **kwargs):
        return {
            "success": True,
            "requested_url": url,
            "final_url": url,
            "status_code": 200,
            "render_method": "http",
            "title": f"Title for {url}",
            "markdown": f"# Content for {url}",
            "content": f"Content for {url}",
            "word_count": 3,
        }

    urls = [
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/3",
    ]

    with patch("smartfetch.batch.smart_fetch", side_effect=mock_fetch):
        res = batch_fetch_urls(urls, max_chars_per_url=5000)

        assert res["success"] is True
        assert res["total_urls"] == 3
        assert res["succeeded_count"] == 3
        assert res["failed_count"] == 0
        assert len(res["results"]) == 3
        for idx, u in enumerate(urls):
            assert res["results"][idx]["requested_url"] == u
            assert res["results"][idx]["success"] is True


def test_batch_fetch_fair_settlement_all_fail():
    """Fair settlement: If ALL URLs in the batch fail, RetrievalFailure is raised (triggers 502, no payment)."""
    with patch("smartfetch.batch.smart_fetch", side_effect=RuntimeError("DNS resolution failed")):
        with pytest.raises(RetrievalFailure, match="All 2 URLs failed in batch retrieval"):
            batch_fetch_urls(["https://example.com/fail1", "https://example.com/fail2"])


def test_batch_fetch_partial_success():
    """If at least 1 URL succeeds, the batch returns success=True with succeeded_count >= 1."""
    def side_effect(url, **kwargs):
        if "fail" in url:
            raise RuntimeError("Upstream 404")
        return {
            "success": True,
            "requested_url": url,
            "final_url": url,
            "status_code": 200,
            "render_method": "http",
            "title": "Success",
            "markdown": "# Success",
            "content": "Success",
            "word_count": 1,
        }

    urls = ["https://example.com/ok", "https://example.com/fail"]
    with patch("smartfetch.batch.smart_fetch", side_effect=side_effect):
        res = batch_fetch_urls(urls)
        assert res["success"] is True
        assert res["total_urls"] == 2
        assert res["succeeded_count"] == 1
        assert res["failed_count"] == 1
        assert res["results"][0]["success"] is True
        assert res["results"][1]["success"] is False


# =====================================================================
# 3. Server Endpoints & Emergency Dispatch
# =====================================================================

def test_batch_fetch_endpoint_emergency_fail_closed():
    """When remote browser is not configured, POST /batch-fetch returns 503."""
    with patch("smartfetch.remote_browser.RemoteBrowserConfig.is_configured", False):
        app = create_app(FREE_SETTINGS)
        with TestClient(app) as client:
            resp = client.post("/batch-fetch", json={"urls": ["https://example.com/"]})
            assert resp.status_code == 503
            assert resp.headers.get("retry-after") == "3600"
            assert resp.json()["error_code"] == "retrieval_unavailable"


def test_batch_fetch_endpoint_402_challenge():
    """In operational mode, an unauthenticated request to POST /batch-fetch returns 402 with amount=15000 ($0.015)."""
    with patch("smartfetch.remote_browser.RemoteBrowserConfig.is_configured", True):
        app = create_app(PAID_SETTINGS)
        with TestClient(app) as client:
            resp = client.post("/batch-fetch", json={"urls": ["https://example.com/"]})
            assert resp.status_code == 402
            req_header = resp.headers.get("payment-required")
            assert req_header is not None
            import base64
            challenge = json.loads(base64.b64decode(req_header).decode("utf-8"))
            assert challenge["x402Version"] == 2
            accepts = challenge["accepts"][0]
            assert accepts["amount"] == "15000"
            assert accepts["network"] == BASE_MAINNET
            assert accepts["payTo"] == VALID_ADDRESS


def test_batch_fetch_endpoint_unpaid_empty_probe_returns_402():
    """An unauthenticated probe `{}` to POST /batch-fetch in operational mode returns 402, not 422/400."""
    with patch("smartfetch.remote_browser.RemoteBrowserConfig.is_configured", True):
        app = create_app(PAID_SETTINGS)
        with TestClient(app) as client:
            resp = client.post("/batch-fetch", json={})
            assert resp.status_code == 402


def test_batch_fetch_endpoint_502_on_retrieval_failure():
    """If batch retrieval fails for all URLs, endpoint returns 502 Bad Gateway (unsettled)."""
    with patch("smartfetch.remote_browser.RemoteBrowserConfig.is_configured", True), \
         patch("smartfetch.server.batch_fetch_urls", side_effect=RetrievalFailure("All URLs failed")):
        app = create_app(FREE_SETTINGS)
        with TestClient(app) as client:
            resp = client.post("/batch-fetch", json={"urls": ["https://example.com/"]})
            assert resp.status_code == 502
            assert resp.json()["error_code"] == "batch_retrieval_failed"


# =====================================================================
# 4. MCP Server & Discovery Manifests
# =====================================================================

def test_mcp_tools_list_has_seven_tools():
    """MCP server lists exactly 7 tools including batch_fetch_webpages."""
    with patch("smartfetch.remote_browser.RemoteBrowserConfig.is_configured", True):
        app = create_app(FREE_SETTINGS)
        with TestClient(app) as client:
            # MCP initialize
            init_res = client.post("/mcp", json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0"},
                },
            }, headers={"Accept": "application/json"})
            assert init_res.status_code == 200

            # List tools
            tools_res = client.post("/mcp", json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }, headers={"Accept": "application/json"})
            assert tools_res.status_code == 200
            tools = tools_res.json()["result"]["tools"]
            assert len(tools) == 7
            tool_names = [t["name"] for t in tools]
            assert "batch_fetch_webpages" in tool_names


def test_discovery_manifests_include_batch_fetch():
    """Discovery manifests (/openapi.json, /.well-known/x402, /.well-known/mcp.json, /meta, /docs, /llms.txt) include batch fetch."""
    with patch("smartfetch.remote_browser.RemoteBrowserConfig.is_configured", True):
        app = create_app(PAID_SETTINGS)
        with TestClient(app) as client:
            # 1. OpenAPI
            openapi = client.get("/openapi.json").json()
            assert "/batch-fetch" in openapi["paths"]
            batch_op = openapi["paths"]["/batch-fetch"]["post"]
            assert batch_op["operationId"] == "batchFetchWebpages"
            assert batch_op["security"] == []
            assert batch_op["x-x402"]["amount"] == "15000"
            assert batch_op["x-x402"]["price"] == "$0.015"

            # 2. x402 manifest
            x402 = client.get("/.well-known/x402").json()
            assert any(r.endswith("/batch-fetch") for r in x402["resources"])

            # 3. MCP discovery
            mcp_meta = client.get("/.well-known/mcp.json").json()
            assert len(mcp_meta["tools"]) == 7
            assert any(t["name"] == "batch_fetch_webpages" for t in mcp_meta["tools"])

            # 4. Meta
            meta = client.get("/meta").json()
            assert "batch_fetch" in meta
            assert meta["batch_fetch"]["price"] == "$0.015"

            # 5. Docs and LLMS
            docs = client.get("/docs").text
            assert "batch-fetch" in docs
            assert "batch_fetch_webpages" in docs
            assert "$0.015" in docs

            llms = client.get("/llms.txt").text
            assert "batch-fetch" in llms
            assert "batch_fetch_webpages" in llms
            assert "$0.015" in llms
