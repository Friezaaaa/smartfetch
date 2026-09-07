from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from smartfetch.provider_controls import RequestCostBudget
from smartfetch.provider_health import ProviderAdapterError, ProviderCircuitBreaker, ProviderConfig
from smartfetch.provider_types import SearchRequest
from smartfetch.providers.exa import ExaHTTPResponse, ExaSearchProvider, RequestsExaTransport


def response_body(*, total: object = 0.012) -> bytes:
    return json.dumps(
        {
            "results": [
                {
                    "id": "provider-object-must-not-escape",
                    "title": "Official result",
                    "url": "https://docs.example.com/reference",
                    "publishedDate": "2026-09-01T12:00:00Z",
                    "highlights": ["Bounded highlight."],
                    "secretCanary": "raw-result-canary",
                }
            ],
            "costDollars": {"total": total, "search": {"neural": 0.007}},
            "requestId": "provider-request-canary",
        }
    ).encode()


class FakeExaTransport:
    def __init__(self, response: ExaHTTPResponse | BaseException) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def search(self, *, api_key, payload, timeout_seconds, max_response_bytes):
        self.calls.append(
            {
                "api_key": api_key,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def make_provider(
    *,
    mode: str = "results",
    transport: FakeExaTransport | None = None,
    api_key: str | None = "exa-test-secret-canary",
    response_cap: int = 1_048_576,
    cost_cap: int = 20_000,
):
    transport = transport or FakeExaTransport(ExaHTTPResponse(200, response_body()))
    config = ProviderConfig(
        provider="exa",
        api_key=api_key,
        timeout_seconds=10.0,
        max_response_bytes=response_cap,
        max_cost_micro_usd=cost_cap,
    )
    circuit = ProviderCircuitBreaker(provider="exa")
    budget = RequestCostBudget(max_total_micro_usd=cost_cap)
    return ExaSearchProvider(
        mode=mode,
        config=config,
        circuit=circuit,
        budget=budget,
        transport=transport,
    ), transport, circuit, budget


class ExaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_three_modes_send_one_bounded_documented_search(self) -> None:
        for mode in ("results", "answer", "structured"):
            with self.subTest(mode=mode):
                provider, transport, _, budget = make_provider(mode=mode)
                result = await provider.search(
                    SearchRequest(
                        query="official x402 docs",
                        max_results=5,
                        domains=("docs.x402.org",),
                        freshness_after="2026-08-01T00:00:00Z",
                    )
                )
                self.assertEqual(len(transport.calls), 1)
                call = transport.calls[0]
                self.assertEqual(call["timeout_seconds"], 10.0)
                self.assertEqual(call["max_response_bytes"], 1_048_576)
                self.assertEqual(
                    call["payload"],
                    {
                        "query": "official x402 docs",
                        "type": "auto",
                        "numResults": 5,
                        "includeDomains": ["docs.x402.org"],
                        "startPublishedDate": "2026-08-01T00:00:00Z",
                        "contents": {"highlights": {"maxCharacters": 800}},
                    },
                )
                self.assertEqual(result.candidates[0].source_id, "s1")
                self.assertEqual(result.candidates[0].snippet, "Bounded highlight.")
                self.assertEqual(result.usage.cost_micro_usd, 12_000)
                self.assertEqual(budget.spent_micro_usd, 12_000)
                rendered = repr(result)
                self.assertNotIn("raw-result-canary", rendered)
                self.assertNotIn("provider-request-canary", rendered)

    async def test_missing_configuration_and_open_circuit_fail_before_transport(self) -> None:
        provider, transport, _, budget = make_provider(api_key=None)
        with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
            await provider.search(SearchRequest("q", 1, (), None))
        self.assertEqual(transport.calls, [])
        self.assertEqual(budget.reserved_micro_usd, 0)

        provider, transport, circuit, _ = make_provider()
        circuit.record_failure(circuit.begin_call())
        with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
            await provider.search(SearchRequest("q", 1, (), None))
        self.assertEqual(transport.calls, [])

    async def test_budget_rejection_happens_before_transport(self) -> None:
        transport = FakeExaTransport(ExaHTTPResponse(200, response_body()))
        config = ProviderConfig("exa", "key", 10.0, 1024, 20_000)
        provider = ExaSearchProvider(
            mode="results",
            config=config,
            circuit=ProviderCircuitBreaker(provider="exa"),
            budget=RequestCostBudget(max_total_micro_usd=19_999),
            transport=transport,
        )
        with self.assertRaisesRegex(ProviderAdapterError, "capacity_unavailable"):
            await provider.search(SearchRequest("q", 1, (), None))
        self.assertEqual(transport.calls, [])

    async def test_timeout_http_and_malformed_output_are_finite_and_never_retried(self) -> None:
        cases = (
            (TimeoutError("timeout-secret-canary"), "provider_timeout"),
            (ExaHTTPResponse(401, b'{"error":"credential-secret-canary"}'), "provider_unavailable"),
            (ExaHTTPResponse(429, b'{"error":"query-secret-canary"}'), "capacity_unavailable"),
            (ExaHTTPResponse(500, b'{"error":"response-secret-canary"}'), "search_failed"),
            (ExaHTTPResponse(200, b"not-json-provider-secret-canary"), "invalid_provider_output"),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected):
                transport = FakeExaTransport(raw)
                provider, _, _, _ = make_provider(transport=transport)
                with self.assertRaises(ProviderAdapterError) as caught:
                    await provider.search(SearchRequest("q", 1, (), None))
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(len(transport.calls), 1)

    async def test_fake_transport_cannot_bypass_response_or_cost_caps(self) -> None:
        provider, transport, _, budget = make_provider(
            transport=FakeExaTransport(ExaHTTPResponse(200, b"x" * 1025)),
            response_cap=1024,
        )
        with self.assertRaisesRegex(ProviderAdapterError, "invalid_provider_output"):
            await provider.search(SearchRequest("q", 1, (), None))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(budget.reserved_micro_usd, 0)

        provider, transport, _, budget = make_provider(
            transport=FakeExaTransport(ExaHTTPResponse(200, response_body(total=0.020001))),
            cost_cap=20_000,
        )
        with self.assertRaisesRegex(ProviderAdapterError, "capacity_unavailable"):
            await provider.search(SearchRequest("q", 1, (), None))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(budget.spent_micro_usd, 0)

    async def test_requests_transport_uses_one_streaming_call_and_enforces_cap(self) -> None:
        class Response:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_content(self, *, chunk_size):
                self.chunk_size = chunk_size
                yield b"1234"
                yield b"56"

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        transport = RequestsExaTransport(session=session)
        result = await transport.search(
            api_key="transport-secret-canary",
            payload={"query": "q"},
            timeout_seconds=10.0,
            max_response_bytes=6,
        )
        self.assertEqual(result.body, b"123456")
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.exa.ai/search")
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["timeout"], (5.0, 10.0))
        self.assertEqual(kwargs["headers"]["x-api-key"], "transport-secret-canary")

        with self.assertRaisesRegex(ProviderAdapterError, "invalid_provider_output"):
            await transport.search(
                api_key="transport-secret-canary",
                payload={"query": "q"},
                timeout_seconds=10.0,
                max_response_bytes=5,
            )
        self.assertEqual(len(session.calls), 2)

    async def test_missing_config_never_reaches_real_requests_boundary(self) -> None:
        with patch("requests.Session.post", side_effect=AssertionError("provider-network-canary")) as post:
            config = ProviderConfig("exa", None, 10.0, 1024, 10_000)
            provider = ExaSearchProvider(
                mode="results",
                config=config,
                circuit=ProviderCircuitBreaker(provider="exa"),
                budget=RequestCostBudget(max_total_micro_usd=10_000),
            )
            with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
                await provider.search(SearchRequest("q", 1, (), None))
            post.assert_not_called()

    async def test_invalid_inputs_and_provider_fields_fail_closed(self) -> None:
        provider, transport, _, _ = make_provider()
        for request in (
            SearchRequest("", 1, (), None),
            SearchRequest("q", True, (), None),
            SearchRequest("q", 11, (), None),
            SearchRequest("q", 1, (), "not-a-timestamp"),
            SearchRequest("q", 1, (), "2026-09-01T01:00:00+01:00"),
        ):
            with self.assertRaisesRegex(ProviderAdapterError, "invalid_provider_output"):
                await provider.search(request)
        self.assertEqual(transport.calls, [])

        bad = json.dumps(
            {
                "results": [{"title": "x", "url": "http://private.invalid", "highlights": []}],
                "costDollars": {"total": 0.001},
            }
        ).encode()
        provider, _, _, _ = make_provider(transport=FakeExaTransport(ExaHTTPResponse(200, bad)))
        with self.assertRaisesRegex(ProviderAdapterError, "invalid_provider_output"):
            await provider.search(SearchRequest("q", 1, (), None))

        malformed_url = json.dumps(
            {
                "results": [{"title": "x", "url": "https://[malformed", "highlights": []}],
                "costDollars": {"total": 0.001},
            }
        ).encode()
        provider, _, _, _ = make_provider(
            transport=FakeExaTransport(ExaHTTPResponse(200, malformed_url))
        )
        with self.assertRaisesRegex(ProviderAdapterError, "invalid_provider_output"):
            await provider.search(SearchRequest("q", 1, (), None))


if __name__ == "__main__":
    unittest.main()
