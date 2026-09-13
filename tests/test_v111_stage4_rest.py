import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from x402.http.utils import (
    decode_payment_required_header,
    encode_payment_signature_header,
)
from x402.schemas import (
    PaymentPayload,
    ResourceVerifyResponse,
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

from smartfetch import payments, server
from smartfetch.payments import BASE_SEPOLIA, X402Settings
from smartfetch.v111_contracts import V111_VARIANTS
from smartfetch.v111_service import V111ExecutionError
from tests.test_v111_stage4_activation import complete_runtime


PAYEE = "0x1111111111111111111111111111111111111111"
PAID = X402Settings(True, PAYEE, "$0.005", BASE_SEPOLIA)
FREE = X402Settings(False, None, "$0.005", BASE_SEPOLIA)
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme="exact",
    network=BASE_SEPOLIA,
)])
EXPECTED_ATOMIC = {
    "results": "50000",
    "answer": "100000",
    "structured": "150000",
    "webpage": "50000",
    "image": "50000",
    "pdf": "50000",
    "audio": "100000",
    "video": "150000",
}


def valid_body(variant):
    if variant.capability == "search_and_extract":
        body = {"query": "current public facts", "max_results": 3}
        if variant.variant == "structured":
            body.update({
                "max_sources": 2,
                "json_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            })
        return body
    body = {
        "source_url": "https://example.com/source",
        "json_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    }
    if variant.variant == "webpage":
        body["render_mode"] = "auto"
    return body


class V111RestIntegrationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "x402.http.HTTPFacilitatorClient.get_supported",
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def active_app(self):
        return server.create_app(
            PAID,
            v111_runtime=complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )

    def test_disabled_surface_is_normal_404_and_not_advertised(self):
        app = server.create_app(
            PAID,
            v111_runtime=complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "false"},
        )
        with TestClient(app) as client:
            for variant in V111_VARIANTS:
                response = client.post(variant.rest_path, json=valid_body(variant))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error_code"], "not_found")
                self.assertNotIn("payment-required", response.headers)
            self.assertFalse(any(
                definition.rest_path in client.get("/openapi.json").text
                for definition in V111_VARIANTS
            ))

    def test_x402_disabled_prevents_activation_even_with_true_flag(self):
        app = server.create_app(
            FREE,
            v111_runtime=complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )
        with TestClient(app) as client:
            self.assertEqual(
                client.post(V111_VARIANTS[0].rest_path, json=valid_body(V111_VARIANTS[0])).status_code,
                404,
            )

    def test_each_valid_variant_has_exact_static_unpaid_challenge(self):
        app = self.active_app()
        with TestClient(app, base_url="https://agent.example") as client:
            for variant in V111_VARIANTS:
                with self.subTest(variant=variant.variant):
                    response = client.post(variant.rest_path, json=valid_body(variant))
                    self.assertEqual(response.status_code, 402)
                    challenge = decode_payment_required_header(
                        response.headers["payment-required"]
                    )
                    accepted = challenge.accepts[0]
                    self.assertEqual(accepted.scheme, "exact")
                    self.assertEqual(accepted.network, BASE_SEPOLIA)
                    self.assertEqual(accepted.amount, EXPECTED_ATOMIC[variant.variant])
                    self.assertEqual(accepted.pay_to, PAYEE)
                    self.assertEqual(
                        str(challenge.resource.url),
                        f"https://agent.example{variant.rest_path}",
                    )

    def test_invalid_input_and_open_circuit_fail_before_challenge(self):
        runtime = complete_runtime()
        app = server.create_app(
            PAID,
            v111_runtime=runtime,
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )
        runtime.exa_circuit.record_failure(runtime.exa_circuit.begin_call())
        with TestClient(app) as client:
            invalid = client.post("/search-and-extract/results", json={"query": ""})
            unavailable = client.post(
                "/search-and-extract/results",
                json={"query": "valid query"},
            )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error_code"], "invalid_request")
        self.assertNotIn("payment-required", invalid.headers)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["error_code"], "provider_unavailable")
        self.assertNotIn("payment-required", unavailable.headers)

    def test_unpaid_requests_never_acquire_provider_permits_or_execute(self):
        runtime = complete_runtime()
        runtime = complete_runtime(
            search_provider_factory=AsyncMock(side_effect=AssertionError("provider")),
        )
        with (
            patch.object(runtime.exa_circuit, "begin_call", wraps=runtime.exa_circuit.begin_call) as begin,
            TestClient(server.create_app(
                PAID,
                v111_runtime=runtime,
                v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
            )) as client,
        ):
            response = client.post(
                "/search-and-extract/results",
                json={"query": "valid query"},
            )
        self.assertEqual(response.status_code, 402)
        begin.assert_not_called()

    def _captured_http_app(self, runtime):
        original = payments.create_x402_resource_server
        captured = []

        def capture(settings, *, register_bazaar=False):
            instance = original(settings, register_bazaar=register_bazaar)
            captured.append(instance)
            return instance

        with patch(
            "smartfetch.payments.create_x402_resource_server",
            side_effect=capture,
        ):
            app = server.create_app(
                PAID,
                v111_runtime=runtime,
                v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
            )
        self.assertEqual(len(captured), 1)
        return app, captured[0]

    def test_paid_success_executes_once_and_settles_once(self):
        runtime = complete_runtime()
        app, resource = self._captured_http_app(runtime)
        service = app.state.v111_activation.service
        with TestClient(app) as client:
            unpaid = client.post(
                "/search-and-extract/results", json={"query": "valid query"}
            )
            requirement = decode_payment_required_header(
                unpaid.headers["payment-required"]
            ).accepts[0]
            resource.verify_payment = AsyncMock(return_value=ResourceVerifyResponse(
                verify=VerifyResponse(isValid=True, payer=PAYEE)
            ))
            resource.settle_payment = AsyncMock(return_value=SettleResponse(
                success=True,
                payer=PAYEE,
                transaction="0xabc",
                network=BASE_SEPOLIA,
            ))
            result = type("Result", (), {
                "model_dump": lambda self, **_kwargs: {
                    "success": True,
                    "mode": "results",
                    "request_id": "test-request",
                    "service_version": "1.10.6",
                    "retrieved_at": "2026-09-13T00:00:00Z",
                    "query": "valid query",
                    "results": [{
                        "source_id": "s1", "rank": 1, "title": "Example",
                        "url": "https://example.com/", "snippet": "Example",
                        "published_at": None,
                    }],
                }
            })()
            with patch.object(
                service, "execute_search", new_callable=AsyncMock,
                return_value=result,
            ) as execute:
                payment = PaymentPayload(
                    accepted=requirement,
                    payload={"authorization": "opaque-test-value"},
                )
                paid = client.post(
                    "/search-and-extract/results",
                    headers={
                        "PAYMENT-SIGNATURE": encode_payment_signature_header(payment)
                    },
                    json={"query": "valid query"},
                )
        self.assertEqual(paid.status_code, 200)
        execute.assert_awaited_once()
        resource.verify_payment.assert_awaited_once()
        resource.settle_payment.assert_awaited_once()

    def test_post_verification_circuit_race_does_not_execute_or_settle(self):
        runtime = complete_runtime()
        app, resource = self._captured_http_app(runtime)
        with TestClient(app) as client:
            unpaid = client.post(
                "/search-and-extract/results", json={"query": "valid query"}
            )
            requirement = decode_payment_required_header(
                unpaid.headers["payment-required"]
            ).accepts[0]

            async def verify(*_args, **_kwargs):
                runtime.exa_circuit.record_failure(
                    runtime.exa_circuit.begin_call()
                )
                return ResourceVerifyResponse(verify=VerifyResponse(
                    isValid=True, payer=PAYEE
                ))

            resource.verify_payment = AsyncMock(side_effect=verify)
            resource.settle_payment = AsyncMock()
            payment = PaymentPayload(
                accepted=requirement,
                payload={"authorization": "opaque-test-value"},
            )
            paid = client.post(
                "/search-and-extract/results",
                headers={
                    "PAYMENT-SIGNATURE": encode_payment_signature_header(payment)
                },
                json={"query": "valid query"},
            )
        self.assertEqual(paid.status_code, 503)
        self.assertEqual(paid.json()["error_code"], "provider_unavailable")
        resource.verify_payment.assert_awaited_once()
        resource.settle_payment.assert_not_awaited()

    def test_validated_delivery_failures_use_contract_status_and_never_settle(self):
        runtime = complete_runtime()
        app, resource = self._captured_http_app(runtime)
        service = app.state.v111_activation.service
        with TestClient(app) as client:
            unpaid = client.post(
                "/search-and-extract/results", json={"query": "valid query"}
            )
            requirement = decode_payment_required_header(
                unpaid.headers["payment-required"]
            ).accepts[0]
            resource.verify_payment = AsyncMock(return_value=ResourceVerifyResponse(
                verify=VerifyResponse(isValid=True, payer=PAYEE)
            ))
            resource.settle_payment = AsyncMock()
            with patch.object(
                service, "execute_search", new_callable=AsyncMock,
                side_effect=V111ExecutionError("evidence_validation_failed"),
            ):
                paid = client.post(
                    "/search-and-extract/results",
                    headers={
                        "PAYMENT-SIGNATURE": encode_payment_signature_header(
                            PaymentPayload(
                                accepted=requirement,
                                payload={"authorization": "opaque-test-value"},
                            )
                        )
                    },
                    json={"query": "valid query"},
                )
        self.assertEqual(paid.status_code, 422)
        self.assertEqual(paid.json()["error_code"], "evidence_validation_failed")
        resource.verify_payment.assert_awaited_once()
        resource.settle_payment.assert_not_awaited()

    def test_cheaper_authorization_cannot_execute_expensive_route(self):
        runtime = complete_runtime()
        app, resource = self._captured_http_app(runtime)
        with TestClient(app) as client:
            cheap = decode_payment_required_header(client.post(
                "/search-and-extract/results", json={"query": "valid query"}
            ).headers["payment-required"]).accepts[0]
            resource.verify_payment = AsyncMock()
            resource.settle_payment = AsyncMock()
            response = client.post(
                "/search-and-extract/answer",
                headers={
                    "PAYMENT-SIGNATURE": encode_payment_signature_header(
                        PaymentPayload(
                            accepted=cheap,
                            payload={"authorization": "opaque-test-value"},
                        )
                    )
                },
                json={"query": "valid query"},
            )
        self.assertEqual(response.status_code, 402)
        accepted = decode_payment_required_header(
            response.headers["payment-required"]
        ).accepts[0]
        self.assertEqual(accepted.amount, "100000")
        resource.verify_payment.assert_not_awaited()
        resource.settle_payment.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
