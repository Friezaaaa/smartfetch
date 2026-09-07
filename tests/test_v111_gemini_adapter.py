import json
import unittest

from smartfetch.model_routing import BenchmarkModelRouter, GEMINI_WORKLOADS
from smartfetch.provider_controls import RequestCostBudget
from smartfetch.provider_health import ProviderAdapterError, ProviderCircuitBreaker, ProviderConfig
from smartfetch.provider_types import AnswerRequest, StructuredMediaRequest, StructuredTextRequest
from smartfetch.providers.gemini import (
    GeminiInteractionResponse,
    GeminiProvider,
    GoogleGenAIInteractionsTransport,
    PreparedMediaInput,
)


USAGE = {
    "total_input_tokens": 100,
    "total_output_tokens": 20,
    "total_thought_tokens": 5,
    "total_tool_use_tokens": 0,
    "input_tokens_by_modality": [{"modality": "TEXT", "tokens": 100}],
}


class FakeGeminiTransport:
    def __init__(self, response: GeminiInteractionResponse | BaseException) -> None:
        self.response = response
        self.calls: list[tuple[dict[str, object], float]] = []

    async def create_interaction(self, *, payload, timeout_seconds):
        self.calls.append((payload, timeout_seconds))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class FakeMediaResolver:
    def __init__(self, prepared: PreparedMediaInput) -> None:
        self.prepared = prepared
        self.calls: list[tuple[str, str]] = []

    def resolve(self, *, source_handle: str, source_type: str) -> PreparedMediaInput:
        self.calls.append((source_handle, source_type))
        return self.prepared


def routes(model: str = "gemini-3.8-flash") -> dict[str, str]:
    return {workload: model for workload in GEMINI_WORKLOADS}


def make_provider(
    response: GeminiInteractionResponse | BaseException,
    *,
    api_key: str | None = "gemini-test-secret-canary",
    router: BenchmarkModelRouter | None = None,
    response_cap: int = 65_536,
    cost_cap: int = 100_000,
    text_workload: str = "structured_search",
    media_resolver: FakeMediaResolver | None = None,
):
    transport = FakeGeminiTransport(response)
    circuit = ProviderCircuitBreaker(provider="gemini")
    budget = RequestCostBudget(max_total_micro_usd=cost_cap)
    provider = GeminiProvider(
        config=ProviderConfig("gemini", api_key, 30.0, response_cap, cost_cap),
        circuit=circuit,
        budget=budget,
        router=router or BenchmarkModelRouter(routes=routes()),
        transport=transport,
        text_workload=text_workload,
        media_resolver=media_resolver,
    )
    return provider, transport, circuit, budget


class GeminiAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_uses_one_stateless_tool_free_server_routed_interaction(self) -> None:
        body = json.dumps(
            {
                "answer": "The wrapper protects execution.",
                "claims": [{"text": "Execution is protected.", "citation_ids": ["c1"]}],
                "citations": [{"citation_id": "c1", "source_id": "s1"}],
            }
        )
        provider, transport, _, budget = make_provider(GeminiInteractionResponse(body, USAGE))
        result = await provider.synthesize_answer(AnswerRequest("What changed?", (("s1", "Source text"),)))
        self.assertEqual(result.answer, "The wrapper protects execution.")
        self.assertEqual(result.citations[0].source_id, "s1")
        self.assertEqual(len(transport.calls), 1)
        payload, timeout = transport.calls[0]
        self.assertEqual(timeout, 30.0)
        self.assertEqual(payload["model"], "gemini-3.8-flash")
        self.assertIs(payload["store"], False)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["tools"], [])
        self.assertNotIn("previous_interaction_id", payload)
        self.assertNotIn("background", payload)
        self.assertEqual(payload["generation_config"]["thinking_level"], "low")
        self.assertLessEqual(payload["generation_config"]["max_output_tokens"], 8192)
        self.assertEqual(budget.spent_micro_usd, result.usage.cost_micro_usd)

    async def test_structured_text_is_locally_schema_and_evidence_validated(self) -> None:
        schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        }
        body = json.dumps(
            {
                "data": {"title": "Example"},
                "evidence": [{"field": "/title", "source_id": "s1", "quote": "Example"}],
                "missing_fields": [],
                "uncertainties": [],
            }
        )
        provider, transport, _, _ = make_provider(GeminiInteractionResponse(body, USAGE))
        result = await provider.extract_text(StructuredTextRequest(schema, None, (("s1", "Example page"),)))
        self.assertEqual(result.data, {"title": "Example"})
        payload, _ = transport.calls[0]
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["properties"]["data"], schema)
        self.assertIs(payload["store"], False)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["tools"], [])
        self.assertNotIn("previous_interaction_id", payload)
        self.assertNotIn("background", payload)

    async def test_prepared_media_is_resolved_locally_without_upload_or_download(self) -> None:
        schema = {
            "type": "object",
            "properties": {"color": {"type": "string"}},
            "required": ["color"],
            "additionalProperties": False,
        }
        resolver = FakeMediaResolver(
            PreparedMediaInput(
                content={"type": "image", "data": "bounded-test-data", "mime_type": "image/png"},
            )
        )
        body = json.dumps(
            {
                "data": {"color": "blue"},
                "evidence": [{"field": "/color", "source_id": "s1", "description": "Blue area"}],
                "missing_fields": [],
                "uncertainties": [],
            }
        )
        provider, transport, _, _ = make_provider(
            GeminiInteractionResponse(body, USAGE),
            media_resolver=resolver,
        )
        result = await provider.extract_media(
            StructuredMediaRequest(schema, None, "image", "request-local-handle")
        )
        self.assertEqual(result.data, {"color": "blue"})
        self.assertEqual(resolver.calls, [("request-local-handle", "image")])
        payload, _ = transport.calls[0]
        self.assertEqual(payload["input"][1]["type"], "image")
        self.assertNotIn("uri", payload["input"][1])
        self.assertIs(payload["store"], False)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["tools"], [])
        self.assertNotIn("previous_interaction_id", payload)
        self.assertNotIn("background", payload)

    async def test_missing_media_resolver_fails_before_transport(self) -> None:
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        provider, transport, _, _ = make_provider(GeminiInteractionResponse("{}", USAGE))
        with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
            await provider.extract_media(StructuredMediaRequest(schema, None, "image", "opaque"))
        self.assertEqual(transport.calls, [])

    async def test_media_source_type_is_validated_before_membership(self) -> None:
        class HostileString(str):
            def __hash__(self) -> int:
                raise RuntimeError("source-type-canary")

        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        provider, transport, _, _ = make_provider(GeminiInteractionResponse("{}", USAGE))
        request = StructuredMediaRequest(schema, None, HostileString("image"), "opaque")
        with self.assertRaisesRegex(ProviderAdapterError, "invalid_provider_output"):
            await provider.extract_media(request)
        self.assertEqual(transport.calls, [])

    async def test_missing_config_route_open_circuit_and_budget_fail_before_transport(self) -> None:
        request = AnswerRequest("q", (("s1", "text"),))
        for provider, transport in (
            make_provider(GeminiInteractionResponse("{}", USAGE), api_key=None)[:2],
            make_provider(
                GeminiInteractionResponse("{}", USAGE), router=BenchmarkModelRouter()
            )[:2],
        ):
            with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
                await provider.synthesize_answer(request)
            self.assertEqual(transport.calls, [])

        provider, transport, circuit, _ = make_provider(GeminiInteractionResponse("{}", USAGE))
        circuit.record_failure(circuit.begin_call())
        with self.assertRaisesRegex(ProviderAdapterError, "provider_unavailable"):
            await provider.synthesize_answer(request)
        self.assertEqual(transport.calls, [])

        provider, transport, _, _ = make_provider(GeminiInteractionResponse("{}", USAGE), cost_cap=0)
        with self.assertRaisesRegex(ProviderAdapterError, "capacity_unavailable"):
            await provider.synthesize_answer(request)
        self.assertEqual(transport.calls, [])

    async def test_timeout_invalid_output_and_cost_overrun_are_finite_and_not_retried(self) -> None:
        request = AnswerRequest("q", (("s1", "text"),))
        cases = (
            (TimeoutError("timeout-secret-canary"), "provider_timeout", 65_536, 100_000),
            (GeminiInteractionResponse("not-json-output-secret-canary", USAGE), "invalid_provider_output", 65_536, 100_000),
            (GeminiInteractionResponse("x" * 1025, USAGE), "invalid_provider_output", 1024, 100_000),
            (
                GeminiInteractionResponse(
                    json.dumps({"answer": "a", "claims": [], "citations": []}),
                    {**USAGE, "total_input_tokens": 100_000},
                ),
                "capacity_unavailable",
                65_536,
                10,
            ),
        )
        for raw, expected, response_cap, cost_cap in cases:
            with self.subTest(expected=expected):
                provider, transport, _, _ = make_provider(
                    raw, response_cap=response_cap, cost_cap=cost_cap
                )
                with self.assertRaises(ProviderAdapterError) as caught:
                    await provider.synthesize_answer(request)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(len(transport.calls), 1 if cost_cap else 0)

    async def test_unknown_citations_and_provider_canaries_never_escape(self) -> None:
        body = json.dumps(
            {
                "answer": "answer",
                "claims": [{"text": "claim", "citation_ids": ["c1"]}],
                "citations": [{"citation_id": "c1", "source_id": "provider-secret-canary"}],
                "raw_provider_field": "response-secret-canary",
            }
        )
        provider, _, _, _ = make_provider(GeminiInteractionResponse(body, USAGE))
        with self.assertRaises(ProviderAdapterError) as caught:
            await provider.synthesize_answer(AnswerRequest("q", (("s1", "text"),)))
        self.assertEqual(str(caught.exception), "invalid_provider_output")
        self.assertNotIn("canary", repr(caught.exception))


class GoogleSDKBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_sdk_boundary_receives_stateless_kwargs_without_duplication(self) -> None:
        class UsageObject:
            def model_dump(self, *, exclude_none: bool):
                self.exclude_none = exclude_none
                return dict(USAGE)

        class InteractionObject:
            output_text = json.dumps(
                {
                    "answer": "ok",
                    "claims": [{"text": "ok", "citation_ids": ["c1"]}],
                    "citations": [{"citation_id": "c1", "source_id": "s1"}],
                }
            )
            usage = UsageObject()

        class Interactions:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                return InteractionObject()

        class Client:
            def __init__(self):
                self.aio = type("Aio", (), {})()
                self.aio.interactions = Interactions()

        client = Client()
        sdk_transport = GoogleGenAIInteractionsTransport(
            api_key="sdk-test-secret-canary",
            client=client,
        )
        provider, _, circuit, budget = make_provider(GeminiInteractionResponse("{}", USAGE))
        provider = GeminiProvider(
            config=ProviderConfig("gemini", "sdk-test-secret-canary", 30.0, 65_536, 100_000),
            circuit=circuit,
            budget=budget,
            router=BenchmarkModelRouter(routes=routes()),
            transport=sdk_transport,
            text_workload="structured_search",
        )
        await provider.synthesize_answer(AnswerRequest("q", (("s1", "text"),)))
        self.assertEqual(len(client.aio.interactions.calls), 1)
        sent = client.aio.interactions.calls[0]
        self.assertIs(sent["store"], False)
        self.assertIs(sent["stream"], False)
        self.assertEqual(sent["tools"], [])
        self.assertNotIn("previous_interaction_id", sent)
        self.assertNotIn("background", sent)
        self.assertEqual(sent["timeout"], 30.0)


if __name__ == "__main__":
    unittest.main()
