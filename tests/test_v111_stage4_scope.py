import io
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import activity, server
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

    def test_emergency_activity_is_correlated_and_never_logs_request_content(self):
        canary = "SECRET-QUERY-CANARY-DO-NOT-LOG"
        app = server.create_app(
            PAID,
            v111_runtime=complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )
        output_stream = io.StringIO()
        previous = activity._HANDLER.setStream(output_stream)
        try:
            with TestClient(app) as client:
                response = client.post("/fetch", json={
                    "url": f"https://example.com/{canary}",
                })
                inactive = client.post("/search-and-extract/results", json={"query": canary})
        finally:
            activity._HANDLER.setStream(previous)
        output = output_stream.getvalue()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(inactive.status_code, 404)
        self.assertIn("unavailable_attempt", output)
        self.assertNotIn("payment_challenged", output)
        self.assertNotIn("tool_started", output)
        events = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["surface"], "fetch")
        self.assertRegex(events[0]["request_id"], r"^[0-9a-f]{16}$")
        self.assertNotIn(canary, output)

    def test_default_discovery_and_existing_fetch_identity_are_fail_closed(self):
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
        self.assertEqual(manifest["resources"], [])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "retrieval_unavailable")
        self.assertNotIn("payment-required", response.headers)


if __name__ == "__main__":
    unittest.main()
