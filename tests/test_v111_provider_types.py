import asyncio
import inspect
from pathlib import Path
import unittest

from smartfetch.costs import ProviderUsage, UsageAccountingError
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
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(UsageAccountingError):
                ProviderUsage(**values)


if __name__ == "__main__":
    unittest.main()
