"""Tests for SmartFetch discovery manifests including A2A Agent Card and VerifyMCP owners."""

import pytest
from starlette.testclient import TestClient

from smartfetch.server import create_app
from smartfetch.payments import BASE_MAINNET, X402Settings


VALID_ADDRESS = "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786"


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    monkeypatch.setenv("BROWSER_WORKER_URL", "https://159.89.40.174:8443")
    monkeypatch.setenv("BROWSER_WORKER_HMAC_KEY", "test-key-mock-for-local-testing-1234567890")


@pytest.fixture
def client():
    settings = X402Settings(
        enabled=True,
        pay_to=VALID_ADDRESS,
        price="$0.005",
        network=BASE_MAINNET,
    )
    app = create_app(settings)
    return TestClient(app)


def test_agent_card_manifest_endpoint(client):
    """GET /.well-known/agent-card.json returns valid A2A Agent Card."""
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    data = response.json()

    assert data["name"] == "SmartFetch"
    assert "version" in data
    assert "description" in data
    assert "protocols" in data
    assert "capabilities" in data
    assert "contact" in data

    # Verify protocols structure
    protocols = data["protocols"]
    assert "mcp" in protocols
    assert protocols["mcp"]["transport"] == "streamable-http"
    assert "/mcp" in protocols["mcp"]["url"]

    assert "rest" in protocols
    assert "/fetch" in protocols["rest"]["url"]
    assert "/openapi.json" in protocols["rest"]["openapi"]

    assert "x402" in protocols
    assert protocols["x402"]["asset"] == "USDC"
    assert protocols["x402"]["network"] == "eip155:8453"
    assert protocols["x402"]["price"] == "$0.005"
    assert protocols["x402"]["enabled"] is True

    # Verify capabilities list all tools
    cap_names = [c["name"] for c in data["capabilities"]]
    assert "fetch_webpage" in cap_names
    assert "webpage_to_markdown" in cap_names
    assert "extract_webpage_text" in cap_names
    assert "render_webpage" in cap_names
    assert "screenshot_webpage" in cap_names

    # Verify no secrets or internal infrastructure IPs are leaked
    body_str = response.text
    assert "159.89.40.174" not in body_str
    assert "CDP_API_KEY" not in body_str
    assert "hmac" not in body_str.lower()


def test_owners_manifest_endpoints(client):
    """GET /.well-known/owners.json and /mcp/.well-known/owners.json return VerifyMCP schema."""
    for path in ["/.well-known/owners.json", "/mcp/.well-known/owners.json"]:
        response = client.get(path)
        assert response.status_code == 200, f"Failed for {path}"
        data = response.json()

        assert data["$schema"] == "https://verifymcp.io/schemas/owners-v1.json"
        assert data["owner"] == "SmartFetch"
        assert data["email"] == "smartfetch.contact@gmail.com"
        assert isinstance(data["servers"], list)
        assert len(data["servers"]) >= 1

        server = data["servers"][0]
        assert server["name"] == "smartfetch"
        assert server["transport"] == "streamable-http"
        assert "/mcp" in server["url"]


def test_existing_manifests_regression(client):
    """Verify existing discovery manifests remain fully functional without regression."""
    # /.well-known/x402
    resp_x402 = client.get("/.well-known/x402")
    assert resp_x402.status_code == 200
    data_x402 = resp_x402.json()
    assert data_x402.get("name") == "SmartFetch" or data_x402.get("service") == "SmartFetch"

    # /.well-known/mcp.json
    resp_mcp = client.get("/.well-known/mcp.json")
    assert resp_mcp.status_code == 200
    assert resp_mcp.json()["name"] == "SmartFetch"

    # /.well-known/glama.json
    resp_glama = client.get("/.well-known/glama.json")
    assert resp_glama.status_code == 200

    # /llms.txt
    resp_llms = client.get("/llms.txt")
    assert resp_llms.status_code == 200
    assert "agent-card.json" in resp_llms.text
    assert "owners.json" in resp_llms.text
    assert "agent-payment-guide.md" in resp_llms.text
    assert "smartfetch-playground.pages.dev" in resp_llms.text

    # /openapi.json contains security: [] on POST /fetch
    resp_openapi = client.get("/openapi.json")
    assert resp_openapi.status_code == 200
    fetch_post = resp_openapi.json()["paths"]["/fetch"]["post"]
    assert "security" in fetch_post
    assert fetch_post["security"] == []

    # /docs contains smartfetch-playground.pages.dev
    resp_docs = client.get("/docs")
    assert resp_docs.status_code == 200
    assert "smartfetch-playground.pages.dev" in resp_docs.text

    # /health
    resp_health = client.get("/health")
    assert resp_health.status_code == 200
    assert resp_health.json()["ok"] is True


def test_openapi_contact_info(client):
    """Verify OpenAPI document contains contact info for AgentCash and directories."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    info = resp.json()["info"]
    assert "contact" in info
    assert info["contact"]["name"] == "SmartFetch"
    assert info["contact"]["email"] == "smartfetch.contact@gmail.com"
    assert "smartfetch-playground.pages.dev" in info["contact"]["url"]


def test_favicons(client):
    """Verify /favicon.ico and /favicon.svg return 200 with SVG bytes and cache header."""
    for path in ["/favicon.ico", "/favicon.svg"]:
        resp = client.get(path)
        assert resp.status_code == 200
        assert "<svg" in resp.text
        assert "max-age=86400" in resp.headers.get("cache-control", "")


def test_unpaid_empty_probe_returns_402(client):
    """In operational mode, unauthenticated empty probes to /fetch return 402 challenge."""
    # Empty JSON object probe
    resp = client.post("/fetch", json={})
    assert resp.status_code == 402
    assert "payment-required" in resp.headers or "PAYMENT-REQUIRED" in resp.headers

    # Empty body probe with application/json
    resp_empty = client.post("/fetch", content=b"", headers={"Content-Type": "application/json"})
    assert resp_empty.status_code == 402
    assert "payment-required" in resp_empty.headers or "PAYMENT-REQUIRED" in resp_empty.headers


def test_emergency_mode_probe_rejected_with_422(monkeypatch):
    """In emergency fail-closed mode, empty probes are rejected with 422."""
    monkeypatch.delenv("BROWSER_WORKER_URL", raising=False)
    monkeypatch.delenv("BROWSER_WORKER_HMAC_KEY", raising=False)
    emergency_app = create_app()
    emergency_client = TestClient(emergency_app)

    resp = emergency_client.post("/fetch", json={})
    assert resp.status_code == 422


def test_paid_probe_missing_url_rejected_with_422(client):
    """When payment signature header is present, empty URL is rejected with 422 without settlement."""
    resp = client.post(
        "/fetch",
        json={},
        headers={"payment-signature": "dummy_payment_sig"},
    )
    assert resp.status_code == 422

