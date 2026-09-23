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
            self.assertEqual(meta["mcp"]["tools"], LEGACY_TOOLS)
            self.assertNotIn("capabilities", meta)
            self.assertEqual(manifest["resources"], [
                "http://testserver/fetch",
            ])
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

    def test_enabled_discovery_exposes_eight_paths_and_six_tools(self):
        app = self.app()
        with TestClient(app) as client:
            openapi = client.get("/openapi.json").json()
            meta = client.get("/meta").json()
            manifest = client.get("/.well-known/x402").json()
        expected_paths = {item.rest_path for item in V111_VARIANTS}
        self.assertEqual(set(openapi["paths"]) - {"/fetch"}, expected_paths)
        self.assertEqual(meta["mcp"]["tools"], LEGACY_TOOLS + V111_TOOLS)
        self.assertEqual(
            set(meta["capabilities"]["variants"]),
            {item.variant for item in V111_VARIANTS},
        )
        self.assertEqual(
            set(manifest["resources"][1:]),
            {f"http://testserver{path}" for path in expected_paths},
        )
        self.assertEqual(len(manifest["v111"]["http"]), 8)
        self.assertEqual(len(manifest["v111"]["mcp"]), 8)

    def test_enabled_openapi_payment_metadata_matches_generated_requirements(self):
        app = self.app()
        with TestClient(app) as client:
            document = client.get("/openapi.json").json()
        for definition in V111_VARIANTS:
            with self.subTest(variant=definition.variant):
                operation = document["paths"][definition.rest_path]["post"]
                requirement = app.state.smartfetch_mcp.v111_accepts[
                    definition.mcp_resource
                ][0]
                payment = operation["x-x402"]
                self.assertEqual(payment["scheme"], requirement.scheme)
                self.assertEqual(payment["network"], requirement.network)
                self.assertEqual(payment["amount"], requirement.amount)
                self.assertEqual(payment["asset"], requirement.asset)
                self.assertEqual(payment["payTo"], requirement.pay_to)
                self.assertEqual(payment["resource"], definition.rest_path)
                self.assertEqual(
                    operation["x-payment-info"]["price"]["amount"],
                    f"{int(requirement.amount) / 1_000_000:.6f}",
                )
                self.assertEqual(set(operation["responses"]), {
                    "200", "400", "402", "413", "415", "422", "502",
                    "503", "504",
                })
                self.assertEqual(
                    operation["parameters"][0]["name"],
                    "PAYMENT-SIGNATURE",
                )
                self.assertIn(
                    "PAYMENT-REQUIRED",
                    operation["responses"]["402"]["headers"],
                )
                self.assertIn(
                    "PAYMENT-RESPONSE",
                    operation["responses"]["200"]["headers"],
                )

    def test_rest_schemas_do_not_expose_path_discriminators(self):
        app = self.app()
        with TestClient(app) as client:
            paths = client.get("/openapi.json").json()["paths"]
        for definition in V111_VARIANTS:
            schema = paths[definition.rest_path]["post"]["requestBody"][
                "content"
            ]["application/json"]["schema"]
            self.assertNotIn("mode", schema.get("properties", {}))
            self.assertNotIn("source_type", schema.get("properties", {}))
            if definition.variant in {"results", "answer"}:
                self.assertNotIn("max_sources", schema["properties"])
            if (
                definition.capability == "search_and_extract"
                and definition.variant != "structured"
            ):
                self.assertNotIn("json_schema", schema["properties"])
            if definition.capability == "extract_structured_data":
                self.assertIn("json_schema", schema["properties"])
            if definition.variant != "webpage":
                self.assertNotIn("render_mode", schema["properties"])


if __name__ == "__main__":
    unittest.main()
