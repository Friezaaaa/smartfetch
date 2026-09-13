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


def payment_for(requirement):
    return {
        "x402Version": 2,
        "accepted": requirement.model_dump(by_alias=True, exclude_none=True),
        "payload": {"authorization": "opaque-test-value"},
    }


def paid_call(client, definition, payment, request_id=50):
    return rpc(client, "tools/call", {
        "name": definition.capability,
        "arguments": arguments_for(definition),
        "_meta": {"x402/payment": payment},
    }, request_id)


class V111MCPIntegrationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "x402.http.HTTPFacilitatorClient.get_supported",
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, runtime=None, flag="true"):
        return server.create_app(
            PAID,
            v111_runtime=runtime or complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": flag},
        )

    def test_disabled_lists_exactly_existing_four_tools(self):
        with TestClient(self.app(flag="false")) as client:
            initialize(client)
            listed = rpc(client, "tools/list", {}, 2)
        self.assertEqual(
            [item["name"] for item in listed["result"]["tools"]],
            ["fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage"],
        )

    def test_enabled_lists_exactly_six_tools_and_eight_static_wrappers(self):
        with TestClient(self.app()) as client:
            initialize(client)
            listed = rpc(client, "tools/list", {}, 2)
        self.assertEqual(
            [item["name"] for item in listed["result"]["tools"]],
            [
                "fetch_webpage", "webpage_to_markdown", "extract_webpage_text", "render_webpage",
                "search_and_extract", "extract_structured_data",
            ],
        )
        self.assertEqual(len(self.app().state.smartfetch_mcp.v111_accepts), 8)

    def test_every_variant_selects_its_exact_mcp_resource_and_amount(self):
        with TestClient(self.app()) as client:
            initialize(client)
            for index, definition in enumerate(V111_VARIANTS, start=10):
                result = rpc(client, "tools/call", {
                    "name": definition.capability,
                    "arguments": arguments_for(definition),
                }, index)
                challenge = result["result"]["structuredContent"]
                self.assertTrue(result["result"]["isError"])
                self.assertEqual(challenge["resource"]["url"], definition.mcp_resource)
                self.assertEqual(challenge["accepts"][0]["amount"], EXPECTED_ATOMIC[definition.variant])
                self.assertEqual(challenge["accepts"][0]["payTo"], PAYEE)

    def test_invalid_variant_and_open_circuit_return_free_structured_error(self):
        runtime = complete_runtime()
        runtime.exa_circuit.record_failure(runtime.exa_circuit.begin_call())
        with TestClient(self.app(runtime)) as client:
            initialize(client)
            invalid = rpc(client, "tools/call", {
                "name": "search_and_extract",
                "arguments": {"query": "valid", "mode": "unknown"},
            }, 2)
            unavailable = rpc(client, "tools/call", {
                "name": "search_and_extract",
                "arguments": {"query": "valid", "mode": "results"},
            }, 3)
        for result, code in ((invalid, "invalid_request"), (unavailable, "provider_unavailable")):
            body = result["result"]["structuredContent"]
            self.assertTrue(result["result"]["isError"])
            self.assertEqual(body["error_code"], code)
            self.assertNotIn("accepts", body)

    def test_paid_success_executes_once_and_settles_once(self):
        app = self.app()
        payment_server = app.state.smartfetch_mcp.resource_server
        definition = V111_VARIANTS[0]
        requirement = app.state.smartfetch_mcp.v111_accepts[
            definition.mcp_resource
        ][0]
        payment_server.find_matching_requirements = Mock(return_value=requirement)
        payment_server.verify_payment = AsyncMock(return_value=VerifyResponse(
            isValid=True, payer=PAYEE
        ))
        payment_server.settle_payment = AsyncMock(return_value=SettleResponse(
            success=True, payer=PAYEE, transaction="0xabc", network=BASE_SEPOLIA
        ))
        service = app.state.v111_activation.service
        result = type("Result", (), {
            "model_dump": lambda self, **_kwargs: {
                "success": True, "mode": "results", "request_id": "mcp-test",
                "service_version": "1.10.6",
                "retrieved_at": "2026-09-13T00:00:00Z", "query": "current public facts",
                "results": [{
                    "source_id": "s1", "rank": 1, "title": "Example",
                    "url": "https://example.com/", "snippet": "Example",
                    "published_at": None,
                }],
            }
        })()
        with (
            patch.object(
                service, "execute_search", new_callable=AsyncMock,
                return_value=result,
            ) as execute,
            TestClient(app) as client,
        ):
            initialize(client)
            response = paid_call(client, definition, payment_for(requirement))
        self.assertFalse(response["result"]["isError"])
        execute.assert_awaited_once()
        payment_server.verify_payment.assert_awaited_once()
        payment_server.settle_payment.assert_awaited_once()

    def test_execution_failure_after_verification_never_settles(self):
        app = self.app()
        payment_server = app.state.smartfetch_mcp.resource_server
        definition = V111_VARIANTS[0]
        requirement = app.state.smartfetch_mcp.v111_accepts[
            definition.mcp_resource
        ][0]
        payment_server.find_matching_requirements = Mock(return_value=requirement)
        payment_server.verify_payment = AsyncMock(return_value=VerifyResponse(
            isValid=True, payer=PAYEE
        ))
        payment_server.settle_payment = AsyncMock()
        service = app.state.v111_activation.service
        with (
            patch.object(
                service, "execute_search", new_callable=AsyncMock,
                side_effect=__import__(
                    "smartfetch.v111_service", fromlist=["V111ExecutionError"]
                ).V111ExecutionError("evidence_validation_failed"),
            ),
            TestClient(app) as client,
        ):
            initialize(client)
            response = paid_call(client, definition, payment_for(requirement))
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(
            response["result"]["structuredContent"]["error_code"],
            "evidence_validation_failed",
        )
        payment_server.verify_payment.assert_awaited_once()
        payment_server.settle_payment.assert_not_awaited()

    def test_cheaper_payment_cannot_execute_expensive_mcp_variant(self):
        app = self.app()
        payment_server = app.state.smartfetch_mcp.resource_server
        cheap = app.state.smartfetch_mcp.v111_accepts[
            "mcp://tool/search_and_extract/results"
        ][0]
        expensive = next(
            item for item in V111_VARIANTS
            if item.mcp_resource == "mcp://tool/search_and_extract/answer"
        )
        payment_server.verify_payment = AsyncMock()
        payment_server.settle_payment = AsyncMock()
        with TestClient(app) as client:
            initialize(client)
            response = paid_call(client, expensive, payment_for(cheap))
        self.assertTrue(response["result"]["isError"])
        challenge = response["result"]["structuredContent"]
        self.assertEqual(challenge["resource"]["url"], expensive.mcp_resource)
        self.assertEqual(challenge["accepts"][0]["amount"], "100000")
        payment_server.verify_payment.assert_not_awaited()
        payment_server.settle_payment.assert_not_awaited()

    def test_circuit_race_after_verification_performs_no_provider_work_or_settlement(self):
        runtime = complete_runtime()
        app = self.app(runtime)
        payment_server = app.state.smartfetch_mcp.resource_server
        definition = V111_VARIANTS[0]
        requirement = app.state.smartfetch_mcp.v111_accepts[
            definition.mcp_resource
        ][0]
        payment_server.find_matching_requirements = Mock(return_value=requirement)

        async def verify(*_args, **_kwargs):
            runtime.exa_circuit.record_failure(runtime.exa_circuit.begin_call())
            return VerifyResponse(isValid=True, payer=PAYEE)

        payment_server.verify_payment = AsyncMock(side_effect=verify)
        payment_server.settle_payment = AsyncMock()
        with TestClient(app) as client:
            initialize(client)
            response = paid_call(client, definition, payment_for(requirement))
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(
            response["result"]["structuredContent"]["error_code"],
            "provider_unavailable",
        )
        payment_server.verify_payment.assert_awaited_once()
        payment_server.settle_payment.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
