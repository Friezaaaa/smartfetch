"""Version and human-readable contracts for the private emergency source tree."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from smartfetch import config, server


ROOT = Path(__file__).resolve().parents[1]


def test_all_public_version_surfaces_use_one_emergency_version():
    descriptor = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    with TestClient(server.create_app()) as client:
        health = client.get("/health").json()
        meta = client.get("/meta").json()
        manifest = client.get("/.well-known/x402").json()
        openapi = client.get("/openapi.json").json()
        root = client.get("/").text
        docs = client.get("/docs").text
        llms = client.get("/llms.txt").text
    assert config.SERVICE_VERSION == descriptor["version"] == "1.11.1"
    assert health["version"] == meta["version"] == manifest["version"] == openapi["info"]["version"] == "1.11.1"
    for text in (root, docs, llms):
        assert "1.11.1" in text
        assert "temporarily unavailable" in text.lower()
        assert "payment challenge" in text.lower() or "do not submit a payment" in text.lower()
    assert "temporarily unavailable" in descriptor["description"].lower()


def test_readme_is_emergency_first_and_does_not_offer_live_buyer_instructions():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# SmartFetch V1.11.1 — emergency fail-closed mode")
    for phrase in (
        "retrieval is temporarily unavailable", "browser rendering is temporarily unavailable",
        "no payment challenge", "V1.11 paid capabilities remain disabled",
        "V1.10.6", "Official MCP Registry", "x402scan",
    ):
        assert phrase.lower() in readme.lower()
    assert "docker build -t smartfetch:v1.11.1" in readme
    assert "not a remote published V1.11.1 image" in readme
    assert "PAYMENT-SIGNATURE" not in readme
    assert "npx agentcash@latest fetch" not in readme
    assert "content_trust: untrusted_external" in readme
    assert "future successful retrieved results" in readme


def test_emergency_errors_never_claim_retrieved_content_or_payment():
    with TestClient(server.create_app()) as client:
        ordinary = client.post("/fetch", json={"url": "https://example.com/"})
        browser = client.post("/fetch", json={"url": "https://example.com/", "force_browser": True})
    for result in (ordinary, browser):
        assert result.status_code == 503
        assert "content_trust" not in result.json()
        assert "payment" not in result.text.lower()
