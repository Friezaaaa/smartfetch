import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from smartfetch.costs import ProviderUsage
from smartfetch.model_routing import BenchmarkModelRouter, GEMINI_WORKLOADS
from smartfetch.provider_health import (
    ProviderAdapterError,
    ProviderCircuitBreaker,
    ProviderConfig,
)
from smartfetch.provider_types import (
    AnswerCitation,
    AnswerClaim,
    CitedAnswerResult,
    SearchCandidate,
    SearchProviderResult,
    StructuredModelResult,
)
from smartfetch.providers.gemini import PreparedMediaInput
from smartfetch.v111_contracts import DirectExtractionRequest, SearchAndExtractRequest
from smartfetch.v111_service import (
    V111ExecutionError,
    V111RuntimeConfig,
    V111Service,
)


SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
USAGE_EXA = ProviderUsage(provider="exa", search_queries=1, cost_micro_usd=7_000)
USAGE_GEMINI = ProviderUsage(
    provider="gemini", input_tokens=10, output_tokens=5,
    thinking_tokens=0, tool_use_tokens=0,
    modality_tokens=(("text", 10),), cost_micro_usd=20,
)


class FakeSearchProvider:
    def __init__(self, calls):
        self.calls = calls

    async def search(self, request):
        self.calls.append(("exa", request.max_results))
        return SearchProviderResult((
            SearchCandidate("s1", 1, "Example", "https://example.com/", "Snippet", None),
            SearchCandidate("s2", 2, "Second", "https://example.org/", "Second", None),
        ), USAGE_EXA)


class FakeModelProvider:
    def __init__(self, calls, workload, resolver=None):
        self.calls = calls
        self.workload = workload
        self.resolver = resolver

    async def synthesize_answer(self, request):
        self.calls.append(("gemini", self.workload, tuple(key for key, _ in request.sources)))
        return CitedAnswerResult(
            "Answer", (AnswerClaim("Claim", ("c1",)),),
            (AnswerCitation("c1", "s1"),), USAGE_GEMINI,
        )

    async def extract_text(self, request):
        self.calls.append(("gemini", self.workload, tuple(key for key, _ in request.sources)))
        return StructuredModelResult(
            {"title": "Example"},
            ({"field": "/title", "source_id": "s1", "quote": "Example"},),
            (), (), USAGE_GEMINI,
        )

    async def extract_media(self, request):
        prepared = self.resolver.resolve(
            source_handle=request.source_handle,
            source_type=request.source_type,
        )
        self.calls.append(("gemini", self.workload, prepared.content["type"]))
        return StructuredModelResult(
            {"title": "Example"},
            ({"field": "/title", "source_id": "s1", "description": "Example"},),
            (), (), USAGE_GEMINI,
        )


def runtime(calls, webpage_retriever, media_ingester):
    exa_config = ProviderConfig("exa", "test-key", 10.0, 65_536, 50_000)
    gemini_config = ProviderConfig("gemini", "test-key", 30.0, 65_536, 150_000)
    return V111RuntimeConfig(
        exa_config=exa_config,
        gemini_config=gemini_config,
        exa_circuit=ProviderCircuitBreaker(provider="exa"),
        gemini_circuit=ProviderCircuitBreaker(provider="gemini"),
        router=BenchmarkModelRouter(routes={
            workload: "gemini-3.5-flash-lite" for workload in GEMINI_WORKLOADS
        }),
        max_request_cost_micro_usd=200_000,
        search_provider_factory=lambda mode, budget: FakeSearchProvider(calls),
        model_provider_factory=lambda workload, budget, resolver: FakeModelProvider(
            calls, workload, resolver
        ),
        webpage_retriever=webpage_retriever,
        media_ingester=media_ingester,
    )


class V111ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        async def retrieve(url, render_mode):
            self.calls.append(("retrieve", url, render_mode))
            return {
                "success": True,
                "final_url": url,
                "title": "Example",
                "content": "Example source text",
                "render_method": "http",
            }

        @asynccontextmanager
        async def ingest(url, source_type):
            self.calls.append(("ingest", source_type))
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "media"
                path.write_bytes(b"media")
                mime_types = {
                    "image": "image/png",
                    "pdf": "application/pdf",
                    "audio": "audio/mpeg",
                    "video": "video/mp4",
                }
                yield type("Media", (), {
                    "download": type("Download", (), {
                        "path": path,
                        "mime_type": mime_types[source_type],
                    })(),
                    "metadata": type("Metadata", (), {})(),
                })()

        self.service = V111Service(runtime(self.calls, retrieve, ingest))

    async def test_results_calls_only_exa_and_returns_bounded_public_results(self):
        result = await self.service.execute_search(SearchAndExtractRequest(
            query="query", mode="results", max_results=2,
        ), request_id="abc")
        self.assertEqual(result.mode, "results")
        self.assertEqual([item.source_id for item in result.results], ["s1", "s2"])
        self.assertEqual(self.calls, [("exa", 2)])

    async def test_answer_searches_retrieves_three_at_most_and_maps_citations(self):
        result = await self.service.execute_search(SearchAndExtractRequest(
            query="query", mode="answer", max_results=2,
        ), request_id="abc")
        self.assertEqual(result.answer, "Answer")
        self.assertEqual(result.citations[0].url, "https://example.com/")
        self.assertEqual(self.calls, [
            ("exa", 2),
            ("retrieve", "https://example.com/", "auto"),
            ("retrieve", "https://example.org/", "auto"),
            ("gemini", "answer", ("s1", "s2")),
        ])

    async def test_structured_search_honors_max_sources_and_returns_evidence(self):
        result = await self.service.execute_search(SearchAndExtractRequest(
            query="query", mode="structured", max_results=2, max_sources=1,
            json_schema=SCHEMA,
        ), request_id="abc")
        self.assertEqual(result.data, {"title": "Example"})
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(self.calls, [
            ("exa", 2),
            ("retrieve", "https://example.com/", "auto"),
            ("gemini", "structured_search", ("s1",)),
        ])

    async def test_webpage_extraction_maps_render_mode_to_existing_retriever(self):
        result = await self.service.execute_extraction(DirectExtractionRequest(
            source_type="webpage", source_url="https://example.com/",
            render_mode="always", json_schema=SCHEMA,
        ), request_id="abc")
        self.assertEqual(result.source_type, "webpage")
        self.assertEqual(result.retrieval_method, "http")
        self.assertEqual(self.calls, [
            ("retrieve", "https://example.com/", "always"),
            ("gemini", "webpage", ("s1",)),
        ])

    async def test_media_variants_use_inline_request_local_resolver(self):
        for source_type in ("image", "pdf", "audio", "video"):
            with self.subTest(source_type=source_type):
                self.calls.clear()
                result = await self.service.execute_extraction(
                    DirectExtractionRequest(
                        source_type=source_type,
                        source_url=f"https://example.com/{source_type}",
                        json_schema=SCHEMA,
                    ),
                    request_id="abc",
                )
                self.assertEqual(result.source_type, source_type)
                self.assertEqual(result.retrieval_method, source_type)
                self.assertEqual(self.calls, [
                    ("ingest", source_type),
                    (
                        "gemini",
                        source_type,
                        "document" if source_type == "pdf" else source_type,
                    ),
                ])

    async def test_provider_failure_maps_to_finite_error_without_canary(self):
        canary = "stage4-provider-error-canary"

        class FailingSearchProvider:
            async def search(self, _request):
                raise ProviderAdapterError(
                    "provider_timeout",
                    provider="exa",
                    cause=RuntimeError(canary),
                )

        configured = replace(
            self.service.runtime,
            search_provider_factory=lambda _mode, _budget: FailingSearchProvider(),
        )
        service = V111Service(configured)
        with self.assertRaises(V111ExecutionError) as raised:
            await service.execute_search(
                SearchAndExtractRequest(query="query", mode="results"),
                request_id="abc",
            )
        self.assertEqual(raised.exception.code, "provider_timeout")
        self.assertNotIn(canary, repr(raised.exception))

    async def test_concurrent_requests_own_distinct_cost_budgets(self):
        budgets = []

        def provider_factory(_mode, budget):
            budgets.append(budget)
            return FakeSearchProvider([])

        service = V111Service(replace(
            self.service.runtime,
            search_provider_factory=provider_factory,
        ))
        first, second = await asyncio.gather(
            service.execute_search(
                SearchAndExtractRequest(query="first", mode="results"),
                request_id="request-one",
            ),
            service.execute_search(
                SearchAndExtractRequest(query="second", mode="results"),
                request_id="request-two",
            ),
        )
        self.assertEqual(
            (first.request_id, second.request_id),
            ("request-one", "request-two"),
        )
        self.assertEqual(len(budgets), 2)
        self.assertIsNot(budgets[0], budgets[1])

    def test_readiness_snapshot_does_not_acquire_a_circuit_permit(self):
        exa = self.service.runtime.exa_circuit
        gemini = self.service.runtime.gemini_circuit
        self.service.require_ready("answer")
        exa_permit = exa.begin_call()
        gemini_permit = gemini.begin_call()
        exa.record_success(exa_permit)
        gemini.record_success(gemini_permit)


if __name__ == "__main__":
    unittest.main()
