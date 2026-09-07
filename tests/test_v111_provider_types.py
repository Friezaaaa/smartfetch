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
    AnswerCitation,
    AnswerClaim,
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
                with self.assertRaisesRegex(ValueError, "provider data must be JSON-compatible"):
                    StructuredTextRequest(value, None, ())
                with self.assertRaisesRegex(ValueError, "provider data must be JSON-compatible"):
                    StructuredModelResult(value, (), (), (), ProviderUsage(provider="gemini"))

    def test_all_sequence_fields_take_ownership_without_aliasing(self):
        domains = ["example.com"]
        candidates = [SearchCandidate("s1", 1, "Title", "https://example.com", "Snippet", None)]
        sources = [["s1", "bounded text"]]
        citation_ids = ["c1"]
        claims = [AnswerClaim("claim", citation_ids)]
        citations = [AnswerCitation("c1", "s1")]
        evidence = [{"field": "/value", "nested": ["quote"]}]
        missing_fields = ["/missing"]
        uncertainties = [{"field": "/missing", "reason": ["absent"]}]

        search_request = SearchRequest("query", 5, domains, None)
        search_result = SearchProviderResult(candidates, ProviderUsage(provider="exa"))
        answer_request = AnswerRequest("query", sources)
        answer_claim = AnswerClaim("claim", citation_ids)
        answer_result = CitedAnswerResult("answer", claims, citations, ProviderUsage(provider="gemini"))
        text_request = StructuredTextRequest({}, None, sources)
        second_text_request = StructuredTextRequest({}, None, sources)
        model_result = StructuredModelResult(
            {"value": "ok"}, evidence, missing_fields, uncertainties, ProviderUsage(provider="gemini")
        )

        domains.append("mutated.example")
        candidates.clear()
        sources[0][1] = "mutated"
        sources.append(["s2", "mutated"])
        citation_ids.append("c2")
        claims.clear()
        citations.clear()
        evidence[0]["nested"].append("mutated")
        missing_fields.append("/mutated")
        uncertainties[0]["reason"].append("mutated")

        self.assertEqual(search_request.domains, ("example.com",))
        self.assertEqual(len(search_result.candidates), 1)
        self.assertEqual(answer_request.sources, (("s1", "bounded text"),))
        self.assertEqual(answer_claim.citation_ids, ("c1",))
        self.assertEqual(len(answer_result.claims), 1)
        self.assertEqual(len(answer_result.citations), 1)
        self.assertEqual(text_request.sources, (("s1", "bounded text"),))
        self.assertEqual(second_text_request.sources, (("s1", "bounded text"),))
        self.assertIsNot(text_request.sources, second_text_request.sources)
        self.assertEqual(model_result.evidence[0]["nested"], ["quote"])
        self.assertEqual(model_result.missing_fields, ("/missing",))
        self.assertEqual(model_result.uncertainties[0]["reason"], ["absent"])

    def test_sequence_fields_reject_malformed_or_oversized_containers(self):
        usage = ProviderUsage(provider="gemini")
        malformed = ("text", b"text", {"s1": "text"}, None, 1)
        for value in malformed:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "^invalid_provider_output$"):
                    SearchRequest("query", 5, value, None)
                with self.assertRaisesRegex(ValueError, "^invalid_provider_output$"):
                    AnswerRequest("query", value)
                with self.assertRaisesRegex(ValueError, "^invalid_provider_output$"):
                    StructuredModelResult({}, (), value, (), usage)
        with self.assertRaisesRegex(ValueError, "^invalid_provider_output$"):
            SearchProviderResult([SearchCandidate("s", 1, "t", "u", "s", None)] * 11, usage)
        for value in ((["s1"],), (("s1", 1),), (([], "text"),)):
            with self.subTest(nested=value), self.assertRaisesRegex(ValueError, "^invalid_provider_output$"):
                AnswerRequest("query", value)

    def test_provider_adapter_raw_response_caps_are_an_explicit_precondition(self):
        import smartfetch.provider_types as module

        documentation = inspect.getdoc(module) or ""
        self.assertIn("raw-response caps", documentation)
        self.assertIn("before constructing", documentation)
        self.assertNotIn("bounded JSON", Path(inspect.getfile(module)).read_text(encoding="utf-8"))

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

    def test_provider_name_malformed_shapes_have_one_finite_error(self):
        malformed = ([], {}, True, False, 1, None, "", "x" * 1024)
        for value in malformed:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(UsageAccountingError, "^invalid_provider_usage$") as caught:
                    ProviderUsage(provider=value)
                self.assertEqual(str(caught.exception), "invalid_provider_usage")

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

    def test_usage_rejects_hostile_builtin_subclasses_with_one_finite_error(self):
        class UnhashableText(str):
            __hash__ = None

        class HostileText(str):
            def __hash__(self):
                raise RuntimeError("HASH_CANARY")

            def __eq__(self, other):
                raise RuntimeError("EQUALITY_CANARY")

        class HostileInteger(int):
            def __ge__(self, other):
                raise RuntimeError("COMPARE_CANARY")

            def __le__(self, other):
                raise RuntimeError("COMPARE_CANARY")

            def __add__(self, other):
                raise RuntimeError("ARITHMETIC_CANARY")

        class HostileList(list):
            def __iter__(self):
                raise RuntimeError("ITERATION_CANARY")

            def __len__(self):
                raise RuntimeError("LENGTH_CANARY")

        class HostileTuple(tuple):
            def __iter__(self):
                raise RuntimeError("ITERATION_CANARY")

            def __len__(self):
                raise RuntimeError("LENGTH_CANARY")

        class HostileDict(dict):
            def __iter__(self):
                raise RuntimeError("ITERATION_CANARY")

        invalid = (
            {"provider": UnhashableText("gemini")},
            {"provider": HostileText("gemini")},
            {"provider": "gemini", "input_tokens": HostileInteger(1)},
            {"provider": "gemini", "modality_tokens": ((UnhashableText("text"), 1),)},
            {"provider": "gemini", "modality_tokens": ((HostileText("text"), 1),)},
            {"provider": "gemini", "modality_tokens": (("text", HostileInteger(1)),)},
            {"provider": "gemini", "modality_tokens": HostileList((("text", 1),))},
            {"provider": "gemini", "modality_tokens": HostileTuple((("text", 1),))},
            {"provider": HostileDict()},
        )
        for values in invalid:
            with self.subTest(value_type=type(next(iter(values.values()))).__name__):
                with self.assertRaisesRegex(UsageAccountingError, "^invalid_provider_usage$") as caught:
                    ProviderUsage(**values)
                self.assertEqual(str(caught.exception), "invalid_provider_usage")

    def test_usage_accepts_exact_builtin_boundary_values(self):
        usage = ProviderUsage(
            provider="exa",
            search_queries=MAX_USAGE_COUNT,
            input_tokens=0,
            output_tokens=MAX_USAGE_COUNT,
            thinking_tokens=0,
            tool_use_tokens=MAX_USAGE_COUNT,
            modality_tokens=(("text", 0), ("video", MAX_USAGE_COUNT)),
            cost_micro_usd=MAX_PROVIDER_COST_MICRO_USD,
        )
        self.assertEqual(usage.total_tokens, MAX_USAGE_COUNT * 2)


class ProviderPlainBoundaryTests(unittest.TestCase):
    def test_owned_sequences_reject_hostile_containers_and_string_subclasses(self):
        class HostileList(list):
            def __iter__(self):
                raise RuntimeError("ITERATION_CANARY")

            def __len__(self):
                raise RuntimeError("LENGTH_CANARY")

        class HostileTuple(tuple):
            def __iter__(self):
                raise RuntimeError("ITERATION_CANARY")

            def __len__(self):
                raise RuntimeError("LENGTH_CANARY")

        class HostileText(str):
            def __len__(self):
                raise RuntimeError("LENGTH_CANARY")

            def __hash__(self):
                raise RuntimeError("HASH_CANARY")

        usage = ProviderUsage(provider="gemini")
        cases = (
            lambda: SearchRequest("query", 5, HostileList(["example.com"]), None),
            lambda: SearchRequest("query", 5, HostileTuple(("example.com",)), None),
            lambda: SearchRequest("query", 5, (HostileText("example.com"),), None),
            lambda: AnswerRequest("query", (HostileList(["s1", "text"]),)),
            lambda: AnswerRequest("query", ((HostileText("s1"), "text"),)),
            lambda: AnswerClaim("claim", (HostileText("c1"),)),
            lambda: StructuredModelResult({}, (), (HostileText("/missing"),), (), usage),
        )
        for construct in cases:
            with self.subTest(construct=construct):
                with self.assertRaisesRegex(ValueError, "^invalid_provider_output$") as caught:
                    construct()
                self.assertNotIn("CANARY", str(caught.exception))

    def test_owned_plain_json_rejects_builtin_subclasses_before_using_them(self):
        class HostileDict(dict):
            def items(self):
                raise RuntimeError("ITEMS_CANARY")

            def __iter__(self):
                raise RuntimeError("ITERATION_CANARY")

        class HostileList(list):
            def __iter__(self):
                raise RuntimeError("ITERATION_CANARY")

        for value in (HostileDict({"value": "ok"}), {"value": HostileList(["ok"])}):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "^provider data must be JSON-compatible$") as caught:
                    StructuredModelResult(value, (), (), (), ProviderUsage(provider="gemini"))
                self.assertNotIn("CANARY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
