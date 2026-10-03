import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import server
from smartfetch.payments import BASE_SEPOLIA, X402Settings
from smartfetch.v111_contracts import V111_VARIANTS
from tests.test_v111_stage4_activation import complete_runtime


PAYEE = "0x1111111111111111111111111111111111111111"
PAID = X402Settings(True, PAYEE, "$0.005", BASE_SEPOLIA)
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme="exact",
    network=BASE_SEPOLIA,
)])
LEGACY_TOOLS = [
    "fetch_webpage",
    "webpage_to_markdown",
    "extract_webpage_text",
    "render_webpage",
]
V111_TOOLS = ["search_and_extract", "extract_structured_data"]


class Stage5ConditionalDiscoveryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "x402.http.HTTPFacilitatorClient.get_supported",
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, *, flag="true", runtime=None):
        return server.create_app(
            PAID,
            v111_runtime=complete_runtime() if runtime is None else runtime,
            v111_environ={"SMARTFETCH_V111_ENABLED": flag},
        )

    def test_disabled_discovery_preserves_four_tools_and_zero_v111_resources(self):
        with TestClient(self.app(flag="false")) as client:
            openapi = client.get("/openapi.json").json()
            meta = client.get("/meta").json()
            manifest = client.get("/.well-known/x402").json()
            self.assertEqual(set(openapi["paths"]), {"/fetch"})
            self.assertEqual([tool["name"] for tool in meta["tools"]], LEGACY_TOOLS)
            self.assertTrue(all(tool["availability"] == "unavailable" for tool in meta["tools"]))
            self.assertNotIn("capabilities", meta)
            self.assertEqual(manifest["resources"], [])
            self.assertNotIn("v111", manifest)

    def test_incomplete_configuration_advertises_no_v111_capability(self):
        app = server.create_app(
            PAID,
            v111_runtime=None,
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )
        with TestClient(app) as client:
            self.assertEqual(set(client.get("/openapi.json").json()["paths"]), {
                "/fetch",
            })

    def test_requested_enabled_discovery_still_exposes_no_v111_execution(self):
        app = self.app()
        with TestClient(app) as client:
            openapi = client.get("/openapi.json").json()
            meta = client.get("/meta").json()
            manifest = client.get("/.well-known/x402").json()
        self.assertEqual(set(openapi["paths"]), {"/fetch"})
        self.assertEqual([tool["name"] for tool in meta["tools"]], LEGACY_TOOLS)
        self.assertEqual(manifest["resources"], [])
        self.assertNotIn("capabilities", meta)
        self.assertNotIn("v111", manifest)
        for item in V111_VARIANTS:
            self.assertNotIn(item.rest_path, openapi["paths"])
            self.assertNotIn(item.mcp_resource, [tool["name"] for tool in meta["tools"]])

    def test_enabled_flag_cannot_create_payable_openapi_metadata(self):
        app = self.app()
        with TestClient(app) as client:
            document = client.get("/openapi.json").json()
        self.assertEqual(set(document["paths"]), {"/fetch"})
        operation = document["paths"]["/fetch"]["post"]
        self.assertEqual(operation["x-smartfetch-availability"], "unavailable")
        self.assertFalse(operation["payment_challenge_enabled"])
        for forbidden in ("x-x402", "x-payment-info", "PAYMENT-SIGNATURE",
                          "PAYMENT-REQUIRED", "PAYMENT-RESPONSE"):
            self.assertNotIn(forbidden, str(operation))

    def test_public_fetch_schema_has_no_v111_path_discriminators(self):
        app = self.app()
        with TestClient(app) as client:
            paths = client.get("/openapi.json").json()["paths"]
        self.assertEqual(set(paths), {"/fetch"})
        schema = paths["/fetch"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(set(schema["properties"]), {"url", "max_chars", "force_browser"})
        for name in ("mode", "source_type", "max_sources", "json_schema", "render_mode"):
            self.assertNotIn(name, schema["properties"])


if __name__ == "__main__":
    unittest.main()
