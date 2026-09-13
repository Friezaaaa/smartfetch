import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import server
from smartfetch.payments import BASE_SEPOLIA, X402Settings
from smartfetch.v111_contracts import V111_VARIANTS
from tests.test_v111_stage4_activation import complete_runtime


PAID = X402Settings(
    True,
    "0x1111111111111111111111111111111111111111",
    "$0.005",
    BASE_SEPOLIA,
)
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2, scheme="exact", network=BASE_SEPOLIA,
)])


class V111Stage4ScopeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "x402.http.HTTPFacilitatorClient.get_supported",
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unpaid_activity_is_correlated_and_never_logs_request_content(self):
        canary = "SECRET-QUERY-CANARY-DO-NOT-LOG"
        app = server.create_app(
            PAID,
            v111_runtime=complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )
        with (
            self.assertLogs("smartfetch.activity") as captured,
            TestClient(app) as client,
        ):
            response = client.post(
                "/search-and-extract/results",
                json={"query": canary},
            )
        output = "\n".join(captured.output)
        self.assertEqual(response.status_code, 402)
        self.assertIn("tool_call_attempted", output)
        self.assertIn("payment_challenged", output)
        self.assertIn("search_and_extract", output)
        self.assertIn(response.headers["x-request-id"], output)
        self.assertNotIn(canary, output)

    def test_default_discovery_and_existing_fetch_contract_remain_unchanged(self):
        app = server.create_app(
            PAID,
            v111_runtime=complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "false"},
        )
        with TestClient(app) as client:
            openapi = client.get("/openapi.json").json()
            manifest = client.get("/.well-known/x402").json()
            response = client.post("/fetch", json={"url": "https://example.com/"})
        self.assertEqual(set(openapi["paths"]), {"/fetch"})
        serialized = str(manifest)
        self.assertFalse(any(
            definition.rest_path in serialized for definition in V111_VARIANTS
        ))
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.headers["payment-required"] != "", True)


if __name__ == "__main__":
    unittest.main()
