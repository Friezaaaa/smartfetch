"""Inert provider protocols and normalized internal V1.11 result types."""

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
        raise ValueError("provider data must be bounded JSON") from None


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    max_results: int
    domains: tuple[str, ...]
    freshness_after: str | None


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


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    query: str
    sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    text: str
    citation_ids: tuple[str, ...]


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


@dataclass(frozen=True, slots=True)
class StructuredTextRequest:
    schema: dict[str, Any]
    instructions: str | None
    sources: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _owned_json(self.schema))


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
        object.__setattr__(self, "evidence", tuple(_owned_json(item) for item in self.evidence))
        object.__setattr__(self, "uncertainties", tuple(_owned_json(item) for item in self.uncertainties))


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
