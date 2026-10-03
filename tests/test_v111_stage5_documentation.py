import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import server
from smartfetch.config import SERVICE_VERSION
from smartfetch.payments import BASE_SEPOLIA, X402Settings
from smartfetch.v111_contracts import V111_VARIANTS
from tests.test_v111_stage4_activation import complete_runtime


ROOT = Path(__file__).resolve().parents[1]
PAYEE = "0x1111111111111111111111111111111111111111"
SETTINGS = X402Settings(True, PAYEE, "$0.005", BASE_SEPOLIA)
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme="exact",
    network=BASE_SEPOLIA,
)])


class Stage5DocumentationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "x402.http.HTTPFacilitatorClient.get_supported",
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, enabled=True):
        return server.create_app(
            SETTINGS,
            v111_runtime=complete_runtime(),
            v111_environ={
                "SMARTFETCH_V111_ENABLED": "true" if enabled else "false",
            },
        )

    def test_release_metadata_is_consistently_emergency_v1111(self):
        manifest = json.loads((ROOT / "server.json").read_text("utf-8"))
        readme = (ROOT / "README.md").read_text("utf-8")
        self.assertEqual(SERVICE_VERSION, "1.11.1")
        self.assertEqual(manifest["version"], "1.11.1")
        self.assertIn("SmartFetch V1.11.1", readme)
        self.assertIn("Retrieval is temporarily unavailable", readme)
        self.assertIn("Browser rendering is temporarily unavailable", readme)
        self.assertIn("no payment challenge", readme)
        self.assertIn("V1.11 paid capabilities remain disabled", readme)
        self.assertIn("docker build -t smartfetch:v1.11.1 .", readme)
        self.assertIn("local image tag", readme)
        self.assertNotIn("docker pull", readme)

    def test_tool_provenance_records_historical_rolling_download(self):
        provenance = (ROOT / "benchmarks" / "v111" / "TOOL_PROVENANCE.md").read_text("utf-8")
        self.assertIn("2026-09-15", provenance)
        self.assertIn("rolling publisher URL", provenance)
        self.assertIn("may resolve to different bytes", " ".join(provenance.split()))
        self.assertIn(
            "fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9",
            provenance,
        )
        self.assertIn(
            "19202b23c0043f15ad1b7bce2344f406fd52bd6efd8f995ce02e7392a1cec52f",
            provenance,
        )

    def test_configured_runtime_cannot_advertise_paid_variants(self):
        app = self.app()
        with TestClient(app) as client:
            docs = client.get("/docs").text
            llms = client.get("/llms.txt").text
            openapi = client.get("/openapi.json").json()
        for definition in V111_VARIANTS:
            for rendered in (docs, llms):
                self.assertNotIn(definition.rest_path, rendered)
                self.assertNotIn(definition.mcp_resource, rendered)
            self.assertNotIn(definition.rest_path, openapi["paths"])
        self.assertIn("availability: unavailable", llms)
        self.assertLess(len(llms.encode("utf-8")), 16_000)

    def test_documentation_is_consistently_fail_closed_even_with_runtime_supplied(self):
        app = self.app()
        with TestClient(app) as client:
            root = client.get("/").text
            docs = client.get("/docs").text
            llms = client.get("/llms.txt").text
            openapi = client.get("/openapi.json").json()
            meta = client.get("/meta").json()
            x402 = client.get("/.well-known/x402").json()
        for rendered in (root, docs, llms, json.dumps(meta), json.dumps(openapi), json.dumps(x402)):
            self.assertIn("unavailable", rendered.lower())
            self.assertNotIn("payment-required", rendered.lower())
            self.assertNotIn("payment-signature", rendered.lower())
        self.assertEqual(x402["resources"], [])
        self.assertEqual(len(meta["tools"]), 4)
        for definition in V111_VARIANTS:
            self.assertNotIn(definition.rest_path, openapi["paths"])

    def test_disabled_docs_preserve_legacy_surface(self):
        with TestClient(self.app(enabled=False)) as client:
            docs = client.get("/docs").text
            llms = client.get("/llms.txt").text
        for definition in V111_VARIANTS:
            self.assertNotIn(definition.rest_path, docs)
            self.assertNotIn(definition.mcp_resource, llms)
        self.assertNotIn("search_and_extract", docs + llms)

    def test_llms_links_are_markdown_and_unsupported_claims_are_absent(self):
        with TestClient(self.app()) as client:
            llms = client.get("/llms.txt").text
        links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", llms)
        self.assertGreaterEqual(len(links), 5)
        self.assertTrue(all(link.startswith(("http://", "https://")) for link in links))
        for forbidden in (
            "captcha bypass",
            "authenticated action",
            "agent card",
            "free demo",
            "monitoring service",
        ):
            self.assertNotIn(forbidden, llms.lower())


if __name__ == "__main__":
    unittest.main()
