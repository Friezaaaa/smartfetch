import asyncio
import inspect
import json
from pathlib import Path
import unittest

from smartfetch.costs import (
    MAX_PROVIDER_COST_MICRO_USD,
    MAX_USAGE_COUNT,
    ProviderUsage,
    UsageAccountingError,
)
from smartfetch.provider_types import (
    AnswerRequest,
    CitedAnswerResult,
    ModelProvider,
    SearchCandidate,
    SearchProvider,
    SearchProviderResult,
    SearchRequest,
    StructuredMediaRequest,
    StructuredModelResult,
    StructuredTextRequest,
)


class FakeSearchProvider:
    async def search(self, request):
        return SearchProviderResult(
            candidates=(SearchCandidate("s1", 1, "Title", "https://example.com", "Snippet", None),),
            usage=ProviderUsage(provider="exa", search_queries=1, cost_micro_usd=1000),
        )


class FakeModelProvider:
    async def synthesize_answer(self, request):
        return CitedAnswerResult("Answer", (), (), ProviderUsage(provider="gemini"))

    async def extract_text(self, request):
        return StructuredModelResult({}, (), (), (), ProviderUsage(provider="gemini"))

    async def extract_media(self, request):
        return StructuredModelResult({}, (), (), (), ProviderUsage(provider="gemini"))


class ProviderProtocolTests(unittest.TestCase):
    def test_dictionary_bearing_provider_types_take_ownership_of_nested_data(self):
        schema = {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["stable"]}},
            "additionalProperties": False,
        }
        data = {"status": {"values": ["stable"]}}
        evidence = ({"field": "/status", "nested": {"values": ["stable"]}},)
        uncertainties = ({"field": "/missing", "nested": ["bounded"]},)

        text = StructuredTextRequest(schema, "instructions", (("s1", "bounded text"),))
        media = StructuredMediaRequest(schema, None, "image", "opaque-local-handle")
        result = StructuredModelResult(
            data,
            evidence,
            ("/missing",),
            uncertainties,
            ProviderUsage(provider="gemini"),
        )
        second = StructuredTextRequest(schema, None, (("s1", "bounded text"),))

        schema["properties"]["status"]["enum"].append("mutated")
        data["status"]["values"].append("mutated")
        evidence[0]["nested"]["values"].append("mutated")
        uncertainties[0]["nested"].append("mutated")

        self.assertEqual(text.schema["properties"]["status"]["enum"], ["stable"])
        self.assertEqual(media.schema["properties"]["status"]["enum"], ["stable"])
        self.assertEqual(result.data["status"]["values"], ["stable"])
        self.assertEqual(result.evidence[0]["nested"]["values"], ["stable"])
        self.assertEqual(result.uncertainties[0]["nested"], ["bounded"])
        self.assertIsNot(text.schema, second.schema)
        json.dumps(text.schema)
        json.dumps(result.data)

    def test_dictionary_bearing_provider_types_reject_non_json_and_cycles(self):
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic
        invalid_values = (cyclic, {"bad": object()}, {"bad": float("nan")})
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "provider data must be bounded JSON"):
                    StructuredTextRequest(value, None, ())
                with self.assertRaisesRegex(ValueError, "provider data must be bounded JSON"):
                    StructuredModelResult(value, (), (), (), ProviderUsage(provider="gemini"))

    def test_protocols_are_inert_typed_async_boundaries(self):
        search = FakeSearchProvider()
        model = FakeModelProvider()
        self.assertIsInstance(search, SearchProvider)
        self.assertIsInstance(model, ModelProvider)
        request = SearchRequest("query", 5, (), None)
        result = asyncio.run(search.search(request))
        self.assertEqual(result.candidates[0].source_id, "s1")

    def test_request_and_result_objects_are_frozen(self):
        request = SearchRequest("query", 5, (), None)
        with self.assertRaises((AttributeError, TypeError)):
            request.query = "changed"

    def test_provider_module_has_no_sdk_environment_or_network_hooks(self):
        import smartfetch.provider_types as module

        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        forbidden = [
            "google.genai", "exa_py", "requests", "httpx", "aiohttp",
            "os.environ", "getenv(", "create_task(", "Thread(",
        ]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_all_future_model_request_shapes_are_plain_data(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        text = StructuredTextRequest(schema, "instructions", (("s1", "bounded text"),))
        media = StructuredMediaRequest(schema, None, "image", "opaque-local-handle")
        answer = AnswerRequest("query", (("s1", "bounded text"),))
        self.assertEqual(text.sources[0][0], "s1")
        self.assertEqual(media.source_type, "image")
        self.assertEqual(answer.query, "query")


class CostAccountingTypeTests(unittest.TestCase):
    def test_usage_tracks_bounded_integer_counts_and_micro_usd(self):
        usage = ProviderUsage(
            provider="gemini",
            input_tokens=100,
            output_tokens=50,
            thinking_tokens=25,
            tool_use_tokens=0,
            modality_tokens=(("image", 20), ("text", 80)),
            cost_micro_usd=12345,
        )
        self.assertEqual(usage.total_tokens, 175)
        self.assertEqual(usage.cost_micro_usd, 12345)

    def test_usage_rejects_negative_boolean_non_integer_and_unbounded_values(self):
        invalid = [
            {"provider": "exa", "search_queries": -1},
            {"provider": "gemini", "input_tokens": True},
            {"provider": "gemini", "output_tokens": 10**12},
            {"provider": "gemini", "cost_micro_usd": -1},
            {"provider": "unknown"},
            {"provider": "gemini", "modality_tokens": (("secret", 1),)},
            {"provider": "gemini", "modality_tokens": (("", 1),)},
            {"provider": "gemini", "modality_tokens": (("text", 1), ("text", 2))},
            {"provider": "gemini", "modality_tokens": (("text", True),)},
            {"provider": "gemini", "modality_tokens": (("text", -1),)},
            {"provider": "gemini", "modality_tokens": (("text", MAX_USAGE_COUNT + 1),)},
            {"provider": "gemini", "modality_tokens": (("text", 1.0),)},
            {"provider": "gemini", "cost_micro_usd": 1.0},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(UsageAccountingError):
                ProviderUsage(**values)

    def test_modality_tokens_require_an_immutable_exact_pair_container(self):
        class TupleSubclass(tuple):
            pass

        invalid_containers = (
            [                          # mutable outer container
                ("image", 1),
            ],
            (("image", 1, 2),),      # too many entry values
            (("image",),),           # too few entry values
            (["image", 1],),         # mutable nested entry
            ("image", 1),            # a single pair, not a tuple of pairs
            "image",
            {"image": 1},
            1,
            (([], 1),),
            ((1, 1),),
            TupleSubclass((("image", 1),)),
            (TupleSubclass(("image", 1)),),
        )
        for value in invalid_containers:
            with self.subTest(value=value), self.assertRaisesRegex(
                UsageAccountingError,
                "^invalid_provider_usage$",
            ):
                ProviderUsage(provider="gemini", modality_tokens=value)

    def test_modality_tokens_accept_exact_safe_boundaries(self):
        usage = ProviderUsage(
            provider="gemini",
            modality_tokens=(("text", 0), ("video", MAX_USAGE_COUNT)),
            cost_micro_usd=MAX_PROVIDER_COST_MICRO_USD,
        )
        self.assertEqual(usage.modality_tokens, (("text", 0), ("video", MAX_USAGE_COUNT)))
        self.assertIsInstance(usage.cost_micro_usd, int)
        with self.assertRaises((AttributeError, TypeError)):
            usage.modality_tokens += (("image", 1),)


if __name__ == "__main__":
    unittest.main()
