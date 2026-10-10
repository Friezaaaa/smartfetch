import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse

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
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
EXPECTED_ATOMIC = {
    "results": "50000", "answer": "100000", "structured": "150000",
    "webpage": "50000", "image": "50000", "pdf": "50000",
    "audio": "100000", "video": "150000",
}


def rpc(client, method, params=None, request_id=1):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    response = client.post("/mcp", headers=MCP_HEADERS, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def initialize(client):
    return rpc(client, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "stage4-test", "version": "1"},
    })


def arguments_for(definition):
    if definition.capability == "search_and_extract":
        args = {"query": "current public facts", "mode": definition.variant}
        if definition.variant == "structured":
            args["json_schema"] = {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            }
        return args
    args = {
        "source_type": definition.variant,
        "source_url": "https://example.com/source",
        "json_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    }
    if definition.variant == "webpage":
        args["render_mode"] = "auto"
    return args


def payment_for(requirement, marker="10"):
    return {
        "x402Version": 2,
        "accepted": requirement.model_dump(by_alias=True, exclude_none=True),
        "payload": {
            "authorization": {
                "from": PAYEE,
                "to": PAYEE,
                "value": requirement.amount,
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + marker * 32,
            },
            "signature": "0x" + "11" * 65,
        },
    }


def paid_call(client, definition, payment, request_id=50):
    return rpc(client, "tools/call", {
        "name": definition.capability,
        "arguments": arguments_for(definition),
        "_meta": {"x402/payment": payment},
    }, request_id)


class V111MCPIntegrationTests(unittest.TestCase):
    def app(self, runtime=None, flag="true"):
        return server.create_app(
            PAID, v111_runtime=runtime or complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": flag},
        )

    def _call(self, client, name, arguments=None, payment=None, request_id=50):
        params = {"name": name, "arguments": arguments or {}}
        if payment is not None:
            params["_meta"] = {"x402/payment": payment}
        return rpc(client, "tools/call", params, request_id)

    def _assert_unknown(self, result):
        self.assertEqual(result["error"], {"code": -32602, "message": "Unknown tool"})
        self.assertNotIn("accepts", str(result))
        self.assertNotIn("payment-response", str(result))

    def test_disabled_lists_exactly_existing_four_tools(self):
        with TestClient(self.app(flag="false")) as client:
            initialize(client)
            listed = rpc(client, "tools/list", {}, 2)
        self.assertEqual([item["name"] for item in listed["result"]["tools"]],
                         ["fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage"])

    def test_enabled_lists_exactly_six_tools_and_eight_static_wrappers(self):
        with patch("x402.mcp.create_payment_wrapper", side_effect=AssertionError("paid wrapper entered")) as wrapper:
            app = self.app()
        with TestClient(app) as client:
            initialize(client)
            listed = rpc(client, "tools/list", {}, 2)
        self.assertEqual(len(listed["result"]["tools"]), 4)
        self.assertEqual(app.app.app.state.smartfetch_mcp.accepts, [])
        wrapper.assert_not_called()

    def test_every_variant_selects_its_exact_mcp_resource_and_amount(self):
        with TestClient(self.app()) as client:
            initialize(client)
            for index, definition in enumerate(V111_VARIANTS, start=10):
                self._assert_unknown(self._call(client, definition.capability, request_id=index))

    def test_invalid_variant_and_open_circuit_return_free_structured_error(self):
        runtime = complete_runtime()
        runtime.exa_circuit.record_failure(runtime.exa_circuit.begin_call())
        with TestClient(self.app(runtime)) as client:
            initialize(client)
            for mode in ("unknown", "results"):
                self._assert_unknown(self._call(client, "search_and_extract",
                                                {"query": "valid", "mode": mode}))

    def test_paid_success_executes_once_and_settles_once(self):
        proof = {"authorization": {"nonce": "private-payment-canary"}}
        with TestClient(self.app()) as client:
            initialize(client)
            result = self._call(client, "search_and_extract",
                                {"query": "current public facts", "mode": "results"}, proof)
        self._assert_unknown(result)
        self.assertNotIn("private-payment-canary", str(result))

    def test_execution_failure_after_verification_never_settles(self):
        with TestClient(self.app()) as client:
            initialize(client)
            result = self._call(client, "search_and_extract", {"query": "valid", "mode": "results"})
        self._assert_unknown(result)

    def test_cheaper_payment_cannot_execute_expensive_mcp_variant(self):
        with TestClient(self.app()) as client:
            initialize(client)
            result = self._call(client, "search_and_extract", {"query": "valid", "mode": "answer"},
                                {"accepted": {"amount": "50000"}})
        self._assert_unknown(result)

    def test_circuit_race_after_verification_performs_no_provider_work_or_settlement(self):
        runtime = complete_runtime()
        with patch("smartfetch.v111_service.V111Service.execute_search",
                   new_callable=AsyncMock, side_effect=AssertionError("provider entered")) as provider:
            with TestClient(self.app(runtime)) as client:
                initialize(client)
                result = self._call(client, "search_and_extract", {"query": "valid", "mode": "results"})
        self._assert_unknown(result)
        provider.assert_not_called()

    def test_replayed_authorization_never_executes_or_settles_twice(self):
        proof = {"authorization": {"nonce": "replay-canary"}}
        with TestClient(self.app()) as client:
            initialize(client)
            first = self._call(client, "search_and_extract", {"query": "valid", "mode": "results"}, proof, 70)
            second = self._call(client, "search_and_extract", {"query": "valid", "mode": "results"}, proof, 71)
        self._assert_unknown(first)
        self._assert_unknown(second)

    def test_same_price_authorization_selects_only_paid_retry_mcp_variant(self):
        with TestClient(self.app()) as client:
            initialize(client)
            result = self._call(client, "extract_structured_data", {"source_type": "image"},
                                {"accepted": {"amount": "50000"}}, 80)
        self._assert_unknown(result)

    def test_post_verification_deadline_returns_finite_error_without_settlement(self):
        with TestClient(self.app()) as client:
            initialize(client)
            result = self._call(client, "search_and_extract", {"query": "valid", "mode": "results"},
                                {"accepted": {"amount": "50000"}}, 81)
        self._assert_unknown(result)

if __name__ == "__main__":
    unittest.main()
