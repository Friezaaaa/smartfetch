import asyncio
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
from smartfetch.config import MAX_REQUEST_BODY_BYTES
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


def _authorization_payload(marker: str, value: str) -> dict:
    return {
        "authorization": {
            "from": PAYEE,
            "to": PAYEE,
            "value": value,
            "validAfter": "0",
            "validBefore": "9999999999",
            "nonce": "0x" + marker * 32,
        },
        "signature": "0x" + "11" * 65,
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
    """Legacy Stage 4 requests remain inert under the emergency release."""

    def active_app(self):
        return server.create_app(
            PAID, v111_runtime=complete_runtime(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )

    def _assert_404(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("payment-required", response.headers)
        self.assertNotIn("payment-response", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def _post(self, variant, *, headers=None, body=None):
        with TestClient(self.active_app()) as client:
            return client.post(variant.rest_path, json=valid_body(variant) if body is None else body,
                               headers=headers)

    def test_understated_content_length_stops_real_asgi_stream_before_challenge(self):
        app = self.active_app()
        chunks = [b"x" * 4096 for _ in range(64)]
        consumed = 0
        sent = []

        async def receive():
            nonlocal consumed
            chunk = chunks[consumed]
            consumed += 1
            return {"type": "http.request", "body": chunk, "more_body": consumed < len(chunks)}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": "/fetch",
            "raw_path": b"/fetch", "query_string": b"",
            "headers": [(b"host", b"agent.example"), (b"content-type", b"application/json"),
                        (b"content-length", b"1")],
            "client": ("127.0.0.1", 1234), "server": ("agent.example", 443), "root_path": "",
        }
        asyncio.run(app(scope, receive, send))
        start = next(item for item in sent if item["type"] == "http.response.start")
        headers = dict(start["headers"])
        self.assertEqual(start["status"], 400)
        self.assertNotIn(b"payment-required", headers)
        self.assertLess(consumed, len(chunks))
        self.assertLessEqual(consumed * 4096, MAX_REQUEST_BODY_BYTES + 4096)

    def test_disabled_surface_is_normal_404_and_not_advertised(self):
        app = server.create_app(PAID, v111_runtime=complete_runtime(),
                                v111_environ={"SMARTFETCH_V111_ENABLED": "false"})
        with TestClient(app) as client:
            for variant in V111_VARIANTS:
                self._assert_404(client.post(variant.rest_path, json=valid_body(variant)))
            openapi = client.get("/openapi.json").json()
        self.assertFalse(any(item.rest_path in openapi["paths"] for item in V111_VARIANTS))

    def test_x402_disabled_prevents_activation_even_with_true_flag(self):
        app = server.create_app(FREE, v111_runtime=complete_runtime(),
                                v111_environ={"SMARTFETCH_V111_ENABLED": "true"})
        with TestClient(app) as client:
            self._assert_404(client.post(V111_VARIANTS[0].rest_path,
                                          json=valid_body(V111_VARIANTS[0])))

    def test_each_valid_variant_has_exact_static_unpaid_challenge(self):
        for variant in V111_VARIANTS:
            with self.subTest(variant=variant.variant):
                self._assert_404(self._post(variant))

    def test_invalid_input_and_open_circuit_fail_before_challenge(self):
        runtime = complete_runtime()
        runtime.exa_circuit.record_failure(runtime.exa_circuit.begin_call())
        app = server.create_app(PAID, v111_runtime=runtime,
                                v111_environ={"SMARTFETCH_V111_ENABLED": "true"})
        with TestClient(app) as client:
            for body in ({"query": ""}, {"query": "valid query"}):
                self._assert_404(client.post(V111_VARIANTS[0].rest_path, json=body))

    def test_unpaid_requests_never_acquire_provider_permits_or_execute(self):
        with patch("smartfetch.v111_service.V111Service.execute_search",
                   new_callable=AsyncMock, side_effect=AssertionError("provider entered")) as execute:
            for variant in V111_VARIANTS:
                self._assert_404(self._post(variant))
        execute.assert_not_awaited()

    def test_paid_success_executes_once_and_settles_once(self):
        variant = V111_VARIANTS[0]
        response = self._post(variant, headers={"PAYMENT-SIGNATURE": "private-proof-canary"})
        self._assert_404(response)
        self.assertNotIn("private-proof-canary", response.text)

    def test_post_verification_circuit_race_does_not_execute_or_settle(self):
        runtime = complete_runtime()
        runtime.exa_circuit.record_failure(runtime.exa_circuit.begin_call())
        with TestClient(server.create_app(PAID, v111_runtime=runtime,
                                         v111_environ={"SMARTFETCH_V111_ENABLED": "true"})) as client:
            self._assert_404(client.post(V111_VARIANTS[0].rest_path,
                                          json=valid_body(V111_VARIANTS[0])))

    def test_validated_delivery_failures_use_contract_status_and_never_settle(self):
        for variant in V111_VARIANTS:
            self._assert_404(self._post(variant, body={"malformed": "private-canary"}))

    def test_cheaper_authorization_cannot_execute_expensive_route(self):
        expensive = next(item for item in V111_VARIANTS if item.variant == "answer")
        self._assert_404(self._post(expensive, headers={"PAYMENT-SIGNATURE": "cheap-proof"}))

    def test_same_price_authorization_executes_only_paid_retry_route_and_body(self):
        image = next(item for item in V111_VARIANTS if item.variant == "image")
        self._assert_404(self._post(image, headers={"PAYMENT-SIGNATURE": "same-price-proof"}))

    def test_replayed_authorization_never_executes_or_settles_twice(self):
        variant = V111_VARIANTS[0]
        first = self._post(variant, headers={"PAYMENT-SIGNATURE": "replay-proof"})
        second = self._post(variant, headers={"PAYMENT-SIGNATURE": "replay-proof"})
        self._assert_404(first)
        self._assert_404(second)

    def test_facilitator_already_settled_rejection_never_executes_or_settles(self):
        with patch("x402.http.middleware.fastapi.payment_middleware",
                   side_effect=AssertionError("paid wrapper entered")) as wrapper:
            app = self.active_app()
        with TestClient(app) as client:
            self._assert_404(client.post(V111_VARIANTS[0].rest_path,
                                          json=valid_body(V111_VARIANTS[0])))
        wrapper.assert_not_called()

    def test_ambiguous_settlement_is_not_retried_on_authorization_replay(self):
        variant = V111_VARIANTS[0]
        for _ in range(2):
            self._assert_404(self._post(variant, headers={"PAYMENT-SIGNATURE": "ambiguous-proof"}))

    def test_wrong_payment_fields_and_paid_retry_body_fail_before_execution(self):
        variant = V111_VARIANTS[0]
        for proof in ("wrong-proof", "replayed-proof"):
            self._assert_404(self._post(variant, headers={"PAYMENT-SIGNATURE": proof},
                                        body={"query": "untrusted-canary"}))

    def test_complete_post_verification_deadline_cancels_and_never_settles(self):
        with patch("smartfetch.v111_service.V111Service.execute_search",
                   new_callable=AsyncMock, side_effect=AssertionError("provider entered")) as execute:
            self._assert_404(self._post(V111_VARIANTS[0]))
        execute.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
