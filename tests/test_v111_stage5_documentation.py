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

    def test_release_metadata_is_consistently_v1110(self):
        manifest = json.loads((ROOT / "server.json").read_text("utf-8"))
        readme = (ROOT / "README.md").read_text("utf-8")
        self.assertEqual(SERVICE_VERSION, "1.11.0")
        self.assertEqual(manifest["version"], "1.11.0")
        self.assertIn("SmartFetch V1.11.0", readme)
        self.assertIn("not deployed", readme)
        self.assertIn("not enabled", readme)
        self.assertIn("not benchmark-approved", readme)
        self.assertIn("not Registry-published", readme)

    def test_enabled_docs_and_llms_describe_every_runtime_variant(self):
        app = self.app()
        with TestClient(app) as client:
            docs = client.get("/docs").text
            llms = client.get("/llms.txt").text
        for definition in V111_VARIANTS:
            requirement = app.state.smartfetch_mcp.v111_accepts[
                definition.mcp_resource
            ][0]
            price = f"${int(requirement.amount) / 1_000_000:g}"
            for rendered in (docs, llms):
                self.assertIn(definition.rest_path, rendered)
                self.assertIn(definition.mcp_resource, rendered)
                self.assertIn(price, rendered)
        self.assertIn("six", llms.lower())
        self.assertLess(len(llms.encode("utf-8")), 16_000)

    def test_enabled_documentation_covers_contract_and_payment_boundaries(self):
        with TestClient(self.app()) as client:
            docs = client.get("/docs").text
            llms = client.get("/llms.txt").text
            openapi = client.get("/openapi.json").json()
        combined = f"{docs}\n{llms}".lower()
        for phrase in (
            "cited answer",
            "structured search",
            "webpage",
            "image",
            "pdf",
            "audio",
            "video",
            "evidence",
            "nullable",
            "missing_fields",
            "payment-required",
            "payment-signature",
            "payment-response",
            "no settlement",
            "deadline",
        ):
            self.assertIn(phrase, combined)
        serialized = json.dumps(openapi).lower()
        self.assertIn("maxresults", serialized.replace("_", ""))
        self.assertIn("maxsources", serialized.replace("_", ""))
        self.assertIn("additionalproperties", serialized.replace("_", ""))

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
        self.assertGreaterEqual(len(links), 6)
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
