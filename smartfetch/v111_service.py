"""Request-local V1.11 orchestration and restart-only activation.

The module is inert until an application factory receives a complete injected
runtime and the exact ``SMARTFETCH_V111_ENABLED=true`` gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import SERVICE_VERSION
from .costs import MAX_PROVIDER_COST_MICRO_USD
from .media import MediaFailure, choose_delivery, ingest_remote_media, retrieve_webpage
from .model_routing import BenchmarkModelRouter, ModelRoutingError
from .provider_controls import RequestCostBudget
from .provider_health import ProviderAdapterError, ProviderCircuitBreaker, ProviderConfig
from .provider_types import (
    AnswerRequest,
    SearchRequest,
    StructuredMediaRequest,
    StructuredTextRequest,
)
from .providers.exa import ExaSearchProvider
from .providers.gemini import GeminiProvider, PreparedMediaInput
from .v111_contracts import (
    Citation,
    Claim,
    DirectExtractionRequest,
    EvidenceEntry,
    FailureCode,
    SearchAndExtractRequest,
    SearchAnswerResponse,
    SearchResultItem,
    SearchResultsResponse,
    SourceRecord,
    StructuredResponse,
    Uncertainty,
    V111_VARIANTS,
)


_CONFIG_LOGGER = logging.getLogger("smartfetch.configuration")


@dataclass(frozen=True, slots=True)
class V111RuntimeConfig:
    exa_config: ProviderConfig
    gemini_config: ProviderConfig
    exa_circuit: ProviderCircuitBreaker
    gemini_circuit: ProviderCircuitBreaker
    router: BenchmarkModelRouter
    max_request_cost_micro_usd: int
    search_provider_factory: Callable[..., Any] | None = field(default=None, repr=False)
    model_provider_factory: Callable[..., Any] | None = field(default=None, repr=False)
    webpage_retriever: Callable[..., Any] = field(default=retrieve_webpage, repr=False)
    media_ingester: Callable[..., Any] = field(default=ingest_remote_media, repr=False)

    def locally_valid(self) -> bool:
        return (
            type(self.exa_config) is ProviderConfig
            and self.exa_config.provider == "exa"
            and self.exa_config.configured
            and self.exa_config.max_cost_micro_usd > 0
            and type(self.gemini_config) is ProviderConfig
            and self.gemini_config.provider == "gemini"
            and self.gemini_config.configured
            and self.gemini_config.max_cost_micro_usd > 0
            and type(self.exa_circuit) is ProviderCircuitBreaker
            and self.exa_circuit.provider == "exa"
            and type(self.gemini_circuit) is ProviderCircuitBreaker
            and self.gemini_circuit.provider == "gemini"
            and type(self.router) is BenchmarkModelRouter
            and self.router.configured
            and type(self.max_request_cost_micro_usd) is int
            and self.max_request_cost_micro_usd <= MAX_PROVIDER_COST_MICRO_USD
            and self.max_request_cost_micro_usd
            >= self.exa_config.max_cost_micro_usd
            + self.gemini_config.max_cost_micro_usd
        )

    def new_budget(self) -> RequestCostBudget:
        return RequestCostBudget(max_total_micro_usd=self.max_request_cost_micro_usd)

    def new_search_provider(self, mode: str, budget: RequestCostBudget):
        if self.search_provider_factory is not None:
            return self.search_provider_factory(mode, budget)
        return ExaSearchProvider(
            mode=mode,
            config=self.exa_config,
            circuit=self.exa_circuit,
            budget=budget,
        )

    def new_model_provider(self, workload: str, budget: RequestCostBudget, resolver=None):
        if self.model_provider_factory is not None:
            return self.model_provider_factory(workload, budget, resolver)
        return GeminiProvider(
            config=self.gemini_config,
            circuit=self.gemini_circuit,
            budget=budget,
            router=self.router,
            text_workload=(
                workload if workload in {"structured_search", "webpage"} else "webpage"
            ),
            media_resolver=resolver,
        )


_ERROR_MESSAGES: Mapping[str, str] = {
    "invalid_request": "Invalid request",
    "invalid_schema": "Invalid schema",
    "invalid_filter": "Invalid filter",
    "invalid_source_url": "Invalid source URL",
    "source_too_large": "Source too large",
    "schema_too_large": "Schema too large",
    "unsupported_media_type": "Unsupported media type",
    "invalid_provider_output": "Invalid provider output",
    "schema_validation_failed": "Schema validation failed",
    "evidence_validation_failed": "Evidence validation failed",
    "search_failed": "Search failed",
    "retrieval_failed": "Retrieval failed",
    "model_failed": "Model failed",
    "provider_unavailable": "Provider unavailable",
    "capacity_unavailable": "Capacity unavailable",
    "provider_timeout": "Provider timeout",
    "retrieval_timeout": "Retrieval timeout",
}


class V111ExecutionError(RuntimeError):
    def __init__(self, code: FailureCode) -> None:
        if type(code) is not str or code not in _ERROR_MESSAGES:
            code = "invalid_request"
        self.code = code
        self.public_message = _ERROR_MESSAGES[code]
        super().__init__(code)


def _variant(name: str):
    return next((item for item in V111_VARIANTS if item.variant == name), None)


def _freshness_after(value: str | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    days = {"day": 1, "week": 7, "month": 31, "year": 365}[value]
    return now - timedelta(days=days)


class _StaticMediaResolver:
    def __init__(self, prepared: PreparedMediaInput) -> None:
        self._prepared = prepared

    def resolve(self, *, source_handle: str, source_type: str) -> PreparedMediaInput:
        if source_handle != "request-local-media" or source_type not in {"image", "pdf", "audio", "video"}:
            raise ValueError("invalid_provider_output")
        return self._prepared


class V111Service:
    """Compose the fixed Stage 1–3 interfaces with request-local state."""

    def __init__(self, runtime: V111RuntimeConfig) -> None:
        self.runtime = runtime

    def require_ready(self, variant: str) -> None:
        definition = _variant(variant)
        if definition is None:
            raise V111ExecutionError("invalid_request")
        try:
            if "exa" in definition.providers:
                self.runtime.exa_config.require_configured()
                self.runtime.exa_circuit.require_available()
            if "gemini" in definition.providers:
                self.runtime.gemini_config.require_configured()
                self.runtime.gemini_circuit.require_available()
                workload = "structured_search" if variant == "structured" else variant
                self.runtime.router.select(workload)
        except (ProviderAdapterError, ModelRoutingError):
            raise V111ExecutionError("provider_unavailable") from None

    async def execute_search(
        self,
        request: SearchAndExtractRequest,
        *,
        request_id: str,
    ) -> SearchResultsResponse | SearchAnswerResponse | StructuredResponse:
        if type(request) is not SearchAndExtractRequest:
            raise V111ExecutionError("invalid_request")
        now = datetime.now(timezone.utc)
        budget = self.runtime.new_budget()
        search = self.runtime.new_search_provider(request.mode, budget)
        try:
            search_result = await search.search(SearchRequest(
                request.query,
                request.max_results,
                request.domains or (),
                (
                    _freshness_after(request.freshness, now).isoformat().replace("+00:00", "Z")
                    if request.freshness else None
                ),
            ))
            candidates = search_result.candidates[:request.max_results]
            if not candidates:
                raise V111ExecutionError("search_failed")
            if request.mode == "results":
                return SearchResultsResponse(
                    request_id=request_id,
                    service_version=SERVICE_VERSION,
                    retrieved_at=now,
                    query=request.query,
                    freshness_after=_freshness_after(request.freshness, now),
                    results=tuple(SearchResultItem(
                        source_id=item.source_id,
                        rank=item.rank,
                        title=item.title,
                        url=item.url,
                        snippet=item.snippet,
                        published_at=item.published_at,
                    ) for item in candidates),
                )
            maximum = min(len(candidates), 3 if request.mode == "answer" else request.max_sources or 3)
            records, source_pairs = await self._retrieve(candidates[:maximum])
            model = self.runtime.new_model_provider(
                "answer" if request.mode == "answer" else "structured_search",
                budget,
                None,
            )
            if request.mode == "answer":
                answer = await model.synthesize_answer(AnswerRequest(request.query, source_pairs))
                by_source = {item.source_id: item for item in candidates[:maximum]}
                citations = tuple(Citation(
                    citation_id=item.citation_id,
                    title=by_source[item.source_id].title,
                    url=by_source[item.source_id].url,
                    published_at=by_source[item.source_id].published_at,
                ) for item in answer.citations)
                return SearchAnswerResponse(
                    request_id=request_id,
                    service_version=SERVICE_VERSION,
                    retrieved_at=now,
                    query=request.query,
                    freshness_after=_freshness_after(request.freshness, now),
                    answer=answer.answer,
                    claims=tuple(Claim(text=item.text, citation_ids=item.citation_ids) for item in answer.claims),
                    citations=citations,
                )
            structured = await model.extract_text(StructuredTextRequest(
                request.json_schema,
                request.instructions,
                source_pairs,
            ))
            return self._structured_response(
                structured, request_id=request_id, now=now, sources=records, mode="structured"
            )
        except V111ExecutionError:
            raise
        except ProviderAdapterError as exc:
            raise V111ExecutionError(exc.code) from None
        except Exception:
            raise V111ExecutionError("retrieval_failed") from None

    async def _retrieve(self, candidates) -> tuple[tuple[SourceRecord, ...], tuple[tuple[str, str], ...]]:
        records = []
        pairs = []
        for candidate in candidates:
            result = await self.runtime.webpage_retriever(candidate.url, "auto")
            if type(result) is not dict or result.get("success") is not True:
                raise V111ExecutionError("retrieval_failed")
            method = result.get("render_method")
            content = result.get("content")
            if method not in {"http", "browser"} or type(content) is not str or not content:
                raise V111ExecutionError("retrieval_failed")
            retrieved_at = datetime.now(timezone.utc)
            records.append(SourceRecord(
                source_id=candidate.source_id,
                title=candidate.title,
                url=candidate.url,
                retrieval_method=method,
                retrieved_at=retrieved_at,
            ))
            pairs.append((candidate.source_id, content))
        return tuple(records), tuple(pairs)

    async def execute_extraction(
        self,
        request: DirectExtractionRequest,
        *,
        request_id: str,
    ) -> StructuredResponse:
        if type(request) is not DirectExtractionRequest:
            raise V111ExecutionError("invalid_request")
        now = datetime.now(timezone.utc)
        budget = self.runtime.new_budget()
        try:
            if request.source_type == "webpage":
                result = await self.runtime.webpage_retriever(
                    request.source_url, request.render_mode
                )
                if type(result) is not dict or result.get("success") is not True:
                    raise V111ExecutionError("retrieval_failed")
                method = result.get("render_method")
                content = result.get("content")
                if method not in {"http", "browser"} or type(content) is not str or not content:
                    raise V111ExecutionError("retrieval_failed")
                source = SourceRecord(
                    source_id="s1", title=str(result.get("title") or "")[:300],
                    url=request.source_url, retrieval_method=method,
                    retrieved_at=now,
                )
                model = self.runtime.new_model_provider("webpage", budget, None)
                output = await model.extract_text(StructuredTextRequest(
                    request.json_schema, request.instructions, (("s1", content),)
                ))
                return self._structured_response(
                    output, request_id=request_id, now=now, sources=(source,),
                    source_type="webpage", retrieval_method=method,
                )
            async with self.runtime.media_ingester(
                request.source_url, request.source_type
            ) as media:
                content = Path(media.download.path).read_bytes()
                decision = choose_delivery(
                    source_type=request.source_type,
                    mime_type=media.download.mime_type,
                    content=content,
                    schema=request.json_schema,
                    instructions=request.instructions,
                    prompt="Extract schema-valid data with bounded evidence.",
                )
                item = dict(decision.payload["media"])
                item["type"] = "document" if request.source_type == "pdf" else request.source_type
                metadata = media.metadata
                prepared = PreparedMediaInput(
                    content=item,
                    duration_seconds=getattr(metadata, "duration_seconds", None),
                    page_count=getattr(metadata, "page_count", None),
                )
                model = self.runtime.new_model_provider(
                    request.source_type, budget, _StaticMediaResolver(prepared)
                )
                output = await model.extract_media(StructuredMediaRequest(
                    request.json_schema,
                    request.instructions,
                    request.source_type,
                    "request-local-media",
                ))
            source = SourceRecord(
                source_id="s1", title="", url=request.source_url,
                retrieval_method=request.source_type, retrieved_at=now,
            )
            return self._structured_response(
                output, request_id=request_id, now=now, sources=(source,),
                source_type=request.source_type,
                retrieval_method=request.source_type,
            )
        except V111ExecutionError:
            raise
        except ProviderAdapterError as exc:
            raise V111ExecutionError(exc.code) from None
        except MediaFailure as exc:
            raise V111ExecutionError(exc.code) from None
        except Exception:
            raise V111ExecutionError("retrieval_failed") from None

    @staticmethod
    def _structured_response(
        output,
        *,
        request_id: str,
        now: datetime,
        sources: tuple[SourceRecord, ...],
        mode: str | None = None,
        source_type: str | None = None,
        retrieval_method: str | None = None,
    ) -> StructuredResponse:
        return StructuredResponse(
            request_id=request_id,
            service_version=SERVICE_VERSION,
            retrieved_at=now,
            mode=mode,
            source_type=source_type,
            retrieval_method=retrieval_method,
            data=output.data,
            sources=sources,
            evidence=tuple(EvidenceEntry(**item) for item in output.evidence),
            missing_fields=output.missing_fields,
            uncertainties=tuple(Uncertainty(**item) for item in output.uncertainties),
        )


@dataclass(frozen=True, slots=True)
class V111Activation:
    enabled: bool
    service: V111Service | None


def load_v111_activation(
    environ: Mapping[str, str],
    runtime: V111RuntimeConfig | None,
) -> V111Activation:
    raw = environ.get("SMARTFETCH_V111_ENABLED")
    if raw not in {None, "", "false", "true"}:
        _CONFIG_LOGGER.warning(json.dumps(
            {"event": "v111_config_invalid", "level": "WARNING"},
            separators=(",", ":"),
            sort_keys=True,
        ))
        return V111Activation(False, None)
    if raw != "true" or type(runtime) is not V111RuntimeConfig:
        return V111Activation(False, None)
    try:
        valid = runtime.locally_valid()
    except Exception:
        valid = False
    if not valid:
        return V111Activation(False, None)
    return V111Activation(True, V111Service(runtime))


__all__ = [
    "V111Activation",
    "V111ExecutionError",
    "V111RuntimeConfig",
    "V111Service",
    "load_v111_activation",
]
