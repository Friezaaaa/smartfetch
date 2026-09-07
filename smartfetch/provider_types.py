"""Inert provider protocols and normalized internal V1.11 result types.

Provider adapters must enforce their design-specified raw-response caps before constructing
these objects.  JSON copying here provides compatibility checks
and ownership isolation; it does not impose a raw provider-response limit.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Protocol, runtime_checkable

from .costs import ProviderUsage


def _owned_json(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return deepcopy(value)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise ValueError("provider data must be JSON-compatible") from None


def _owned_sequence(value: Any, *, maximum: int) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ValueError("invalid_provider_output")
    return tuple(value)


def _owned_strings(value: Any, *, maximum: int, max_chars: int) -> tuple[str, ...]:
    items = _owned_sequence(value, maximum=maximum)
    if any(not isinstance(item, str) or not item or len(item) > max_chars for item in items):
        raise ValueError("invalid_provider_output")
    return items


def _owned_sources(value: Any) -> tuple[tuple[str, str], ...]:
    sources = _owned_sequence(value, maximum=3)
    owned: list[tuple[str, str]] = []
    for source in sources:
        if not isinstance(source, (list, tuple)) or len(source) != 2:
            raise ValueError("invalid_provider_output")
        source_id, content = source
        if (
            not isinstance(source_id, str)
            or not 1 <= len(source_id) <= 64
            or not isinstance(content, str)
            or len(content) > 50_000
        ):
            raise ValueError("invalid_provider_output")
        owned.append((source_id, content))
    return tuple(owned)


def _owned_instances(value: Any, expected: type, *, maximum: int) -> tuple[Any, ...]:
    items = _owned_sequence(value, maximum=maximum)
    if any(not isinstance(item, expected) for item in items):
        raise ValueError("invalid_provider_output")
    return items


def _owned_json_objects(value: Any, *, maximum: int) -> tuple[dict[str, Any], ...]:
    items = _owned_sequence(value, maximum=maximum)
    if any(type(item) is not dict for item in items):
        raise ValueError("invalid_provider_output")
    return tuple(_owned_json(item) for item in items)


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    max_results: int
    domains: tuple[str, ...]
    freshness_after: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "domains", _owned_strings(self.domains, maximum=10, max_chars=253))


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    source_id: str
    rank: int
    title: str
    url: str
    snippet: str
    published_at: str | None


@dataclass(frozen=True, slots=True)
class SearchProviderResult:
    candidates: tuple[SearchCandidate, ...]
    usage: ProviderUsage

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", _owned_instances(self.candidates, SearchCandidate, maximum=10))


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    query: str
    sources: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", _owned_sources(self.sources))


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    text: str
    citation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "citation_ids", _owned_strings(self.citation_ids, maximum=20, max_chars=64))


@dataclass(frozen=True, slots=True)
class AnswerCitation:
    citation_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class CitedAnswerResult:
    answer: str
    claims: tuple[AnswerClaim, ...]
    citations: tuple[AnswerCitation, ...]
    usage: ProviderUsage

    def __post_init__(self) -> None:
        object.__setattr__(self, "claims", _owned_instances(self.claims, AnswerClaim, maximum=50))
        object.__setattr__(self, "citations", _owned_instances(self.citations, AnswerCitation, maximum=20))


@dataclass(frozen=True, slots=True)
class StructuredTextRequest:
    schema: dict[str, Any]
    instructions: str | None
    sources: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _owned_json(self.schema))
        object.__setattr__(self, "sources", _owned_sources(self.sources))


@dataclass(frozen=True, slots=True)
class StructuredMediaRequest:
    schema: dict[str, Any]
    instructions: str | None
    source_type: str
    source_handle: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _owned_json(self.schema))


@dataclass(frozen=True, slots=True)
class StructuredModelResult:
    data: Any
    evidence: tuple[dict[str, Any], ...]
    missing_fields: tuple[str, ...]
    uncertainties: tuple[dict[str, Any], ...]
    usage: ProviderUsage

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _owned_json(self.data))
        object.__setattr__(self, "evidence", _owned_json_objects(self.evidence, maximum=100))
        object.__setattr__(self, "missing_fields", _owned_strings(self.missing_fields, maximum=64, max_chars=256))
        object.__setattr__(self, "uncertainties", _owned_json_objects(self.uncertainties, maximum=64))


@runtime_checkable
class SearchProvider(Protocol):
    async def search(self, request: SearchRequest) -> SearchProviderResult: ...


@runtime_checkable
class ModelProvider(Protocol):
    async def synthesize_answer(self, request: AnswerRequest) -> CitedAnswerResult: ...

    async def extract_text(self, request: StructuredTextRequest) -> StructuredModelResult: ...

    async def extract_media(self, request: StructuredMediaRequest) -> StructuredModelResult: ...


__all__ = [
    "AnswerCitation",
    "AnswerClaim",
    "AnswerRequest",
    "CitedAnswerResult",
    "ModelProvider",
    "SearchCandidate",
    "SearchProvider",
    "SearchProviderResult",
    "SearchRequest",
    "StructuredMediaRequest",
    "StructuredModelResult",
    "StructuredTextRequest",
]
