"""Inert V1.11 request, response, and finite-variant contracts.

Nothing in this module registers a route, creates a payment requirement, reads
configuration, or performs I/O.  Later integration stages may map these closed
definitions to the existing SmartFetch payment and retrieval layers.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import ipaddress
import re
import socket
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from .schema_guard import validate_schema


SearchMode = Literal["results", "answer", "structured"]
SourceType = Literal["webpage", "image", "pdf", "audio", "video"]
RenderMode = Literal["auto", "always"]
Freshness = Literal["day", "week", "month", "year"]
RetrievalMethod = Literal["http", "browser", "image", "pdf", "audio", "video"]
FailureCode = Literal[
    "invalid_request",
    "invalid_schema",
    "invalid_filter",
    "invalid_source_url",
    "source_too_large",
    "schema_too_large",
    "unsupported_media_type",
    "invalid_provider_output",
    "schema_validation_failed",
    "evidence_validation_failed",
    "search_failed",
    "retrieval_failed",
    "model_failed",
    "provider_cleanup_failed",
    "provider_unavailable",
    "capacity_unavailable",
    "provider_timeout",
    "retrieval_timeout",
]


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


UTCDateTime = Annotated[datetime, AfterValidator(_require_utc)]


@dataclass(frozen=True, slots=True)
class VariantDefinition:
    capability: Literal["search_and_extract", "extract_structured_data"]
    variant: str
    rest_path: str
    mcp_resource: str
    price: str
    providers: tuple[Literal["exa", "smartfetch", "gemini"], ...]


V111_VARIANTS: tuple[VariantDefinition, ...] = (
    VariantDefinition("search_and_extract", "results", "/search-and-extract/results", "mcp://tool/search_and_extract/results", "$0.05", ("exa",)),
    VariantDefinition("search_and_extract", "answer", "/search-and-extract/answer", "mcp://tool/search_and_extract/answer", "$0.10", ("exa", "smartfetch", "gemini")),
    VariantDefinition("search_and_extract", "structured", "/search-and-extract/structured", "mcp://tool/search_and_extract/structured", "$0.15", ("exa", "smartfetch", "gemini")),
    VariantDefinition("extract_structured_data", "webpage", "/extract-structured-data/webpage", "mcp://tool/extract_structured_data/webpage", "$0.05", ("gemini",)),
    VariantDefinition("extract_structured_data", "image", "/extract-structured-data/image", "mcp://tool/extract_structured_data/image", "$0.05", ("gemini",)),
    VariantDefinition("extract_structured_data", "pdf", "/extract-structured-data/pdf", "mcp://tool/extract_structured_data/pdf", "$0.05", ("gemini",)),
    VariantDefinition("extract_structured_data", "audio", "/extract-structured-data/audio", "mcp://tool/extract_structured_data/audio", "$0.10", ("gemini",)),
    VariantDefinition("extract_structured_data", "video", "/extract-structured-data/video", "mcp://tool/extract_structured_data/video", "$0.15", ("gemini",)),
)


_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _normalized_public_dns_name(value: str) -> str:
    candidate = value.strip().rstrip(".")
    if not candidate or len(candidate) > 253:
        raise ValueError("domain must be a bounded public DNS hostname")
    if any(marker in candidate for marker in ("://", "@", "/", "?", "#", "*")):
        raise ValueError("domain must contain only a hostname")
    try:
        ascii_name = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("domain is not a valid DNS hostname") from exc
    if len(ascii_name) > 253:
        raise ValueError("domain must be a bounded public DNS hostname")
    try:
        ipaddress.ip_address(ascii_name)
    except ValueError:
        try:
            socket.inet_aton(ascii_name)
        except OSError:
            pass
        else:
            raise ValueError("IP literals are not accepted")
    else:
        raise ValueError("IP literals are not accepted")
    labels = ascii_name.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("domain must be a public DNS hostname")
    if labels[-1] in {"localhost", "local", "internal", "invalid", "test"}:
        raise ValueError("domain must be a public DNS hostname")
    return ascii_name


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchAndExtractRequest(_StrictModel):
    query: str = Field(min_length=1, max_length=500)
    mode: SearchMode
    max_results: int = Field(default=5, ge=1, le=10, strict=True)
    max_sources: int | None = Field(default=None, ge=1, le=3, strict=True)
    domains: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=10)
    freshness: Freshness | None = None
    json_schema: dict[str, Any] | None = None
    instructions: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("query", "instructions", mode="before")
    @classmethod
    def _trim_strings(cls, value: Any) -> Any:
        if value is None:
            return None
        if type(value) is not str:
            return value
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value

    @field_validator("domains")
    @classmethod
    def _normalize_domains(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(_normalized_public_dns_name(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("domains must be unique after normalization")
        return normalized

    @field_validator("json_schema")
    @classmethod
    def _guard_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            validate_schema(value)
            return deepcopy(value)
        return None

    @model_validator(mode="after")
    def _validate_mode_fields(self) -> "SearchAndExtractRequest":
        structured = self.mode == "structured"
        if structured and self.json_schema is None:
            raise ValueError("structured mode requires json_schema")
        if not structured and self.json_schema is not None:
            raise ValueError("json_schema is accepted only in structured mode")
        if not structured and self.instructions is not None:
            raise ValueError("instructions are accepted only in structured mode")
        if not structured and self.max_sources is not None:
            raise ValueError("max_sources is accepted only in structured mode")
        if structured:
            max_sources = 3 if self.max_sources is None else self.max_sources
            if max_sources > self.max_results:
                raise ValueError("max_sources cannot exceed max_results")
            object.__setattr__(self, "max_sources", max_sources)
        return self


class DirectExtractionRequest(_StrictModel):
    source_type: SourceType
    source_url: str = Field(min_length=1, max_length=4096)
    render_mode: RenderMode | None = None
    json_schema: dict[str, Any]
    instructions: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("json_schema")
    @classmethod
    def _guard_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_schema(value)
        return deepcopy(value)

    @field_validator("source_url", mode="before")
    @classmethod
    def _trim_source_url(cls, value: Any) -> Any:
        if type(value) is str:
            return value.strip()
        return value

    @field_validator("source_url")
    @classmethod
    def _validate_source_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("source_url must be an HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source_url credentials are not accepted")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("source_url has an invalid port") from exc
        if port not in (None, 443):
            raise ValueError("source_url must not use a nonstandard port")
        _normalized_public_dns_name(parsed.hostname)
        return value

    @field_validator("instructions", mode="before")
    @classmethod
    def _trim_instructions(cls, value: Any) -> Any:
        if value is None:
            return None
        if type(value) is not str:
            return value
        value = value.strip()
        if not value:
            raise ValueError("instructions cannot be blank")
        return value

    @model_validator(mode="after")
    def _validate_render_mode(self) -> "DirectExtractionRequest":
        if self.source_type == "webpage":
            object.__setattr__(self, "render_mode", self.render_mode or "auto")
        elif self.render_mode is not None:
            raise ValueError("render_mode is accepted only for webpage extraction")
        return self


class CommonSuccess(_StrictModel):
    success: Literal[True] = True
    request_id: str = Field(min_length=1, max_length=64)
    service_version: str = Field(min_length=1, max_length=32)
    retrieved_at: UTCDateTime


class SearchResultItem(_StrictModel):
    source_id: str = Field(min_length=1, max_length=64)
    rank: int = Field(ge=1, le=10, strict=True)
    title: str = Field(max_length=300)
    url: str = Field(max_length=4096)
    snippet: str = Field(max_length=800)
    published_at: UTCDateTime | None = None


class Citation(_StrictModel):
    citation_id: str = Field(min_length=1, max_length=64)
    title: str = Field(max_length=300)
    url: str = Field(max_length=4096)
    published_at: UTCDateTime | None = None


class Claim(_StrictModel):
    text: str = Field(min_length=1, max_length=500)
    citation_ids: tuple[str, ...] = Field(min_length=1, max_length=20)


class SourceRecord(_StrictModel):
    source_id: str = Field(min_length=1, max_length=64)
    title: str = Field(max_length=300)
    url: str = Field(max_length=4096)
    retrieval_method: RetrievalMethod
    retrieved_at: UTCDateTime


class EvidenceEntry(_StrictModel):
    field: str = Field(max_length=256)
    source_id: str = Field(min_length=1, max_length=64)
    quote: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, min_length=1, max_length=300)
    page: int | None = Field(default=None, ge=1, le=20, strict=True)
    start_seconds: float | None = Field(default=None, ge=0, le=1800, strict=True)
    end_seconds: float | None = Field(default=None, ge=0, le=1800, strict=True)


class Uncertainty(_StrictModel):
    field: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=300)


class SearchResultsResponse(CommonSuccess):
    mode: Literal["results"] = "results"
    query: str = Field(min_length=1, max_length=500)
    freshness_after: UTCDateTime | None = None
    results: tuple[SearchResultItem, ...] = Field(min_length=1, max_length=10)


class SearchAnswerResponse(CommonSuccess):
    mode: Literal["answer"] = "answer"
    query: str = Field(min_length=1, max_length=500)
    freshness_after: UTCDateTime | None = None
    answer: str = Field(min_length=1, max_length=12000)
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=50)
    citations: tuple[Citation, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _validate_citation_references(self) -> "SearchAnswerResponse":
        citation_ids = [citation.citation_id for citation in self.citations]
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("citation IDs must be unique")
        known = set(citation_ids)
        if any(citation_id not in known for claim in self.claims for citation_id in claim.citation_ids):
            raise ValueError("claims must reference returned citations")
        return self


class StructuredResponse(CommonSuccess):
    mode: Literal["structured"] | None = None
    source_type: SourceType | None = None
    retrieval_method: RetrievalMethod | None = None
    data: Any
    sources: tuple[SourceRecord, ...] = Field(min_length=1, max_length=3)
    evidence: tuple[EvidenceEntry, ...] = Field(max_length=100)
    missing_fields: tuple[str, ...] = Field(max_length=64)
    uncertainties: tuple[Uncertainty, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def _validate_capability_shape(self) -> "StructuredResponse":
        is_search = self.mode == "structured" and self.source_type is None
        is_direct = self.mode is None and self.source_type is not None
        if not (is_search or is_direct):
            raise ValueError("structured response must identify exactly one capability")
        if is_direct and (len(self.sources) != 1 or self.retrieval_method is None):
            raise ValueError("direct extraction requires one source and retrieval_method")
        if is_direct:
            allowed_methods = (
                {"http", "browser"}
                if self.source_type == "webpage"
                else {self.source_type}
            )
            if (
                self.retrieval_method not in allowed_methods
                or self.sources[0].retrieval_method != self.retrieval_method
            ):
                raise ValueError("direct extraction retrieval methods must match the source type")
        if is_search and self.retrieval_method is not None:
            raise ValueError("search response does not use top-level retrieval_method")
        if is_search and any(
            source.retrieval_method not in {"http", "browser"}
            for source in self.sources
        ):
            raise ValueError("structured search sources must use webpage retrieval methods")
        return self


class FailureResponse(_StrictModel):
    success: Literal[False] = False
    error_code: FailureCode
    error: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=1, max_length=64)
    service_version: str = Field(min_length=1, max_length=32)


__all__ = [
    "Citation",
    "Claim",
    "CommonSuccess",
    "DirectExtractionRequest",
    "EvidenceEntry",
    "FailureResponse",
    "FailureCode",
    "Freshness",
    "RenderMode",
    "RetrievalMethod",
    "SearchAndExtractRequest",
    "SearchAnswerResponse",
    "SearchMode",
    "SearchResultItem",
    "SearchResultsResponse",
    "SourceRecord",
    "SourceType",
    "StructuredResponse",
    "Uncertainty",
    "V111_VARIANTS",
    "VariantDefinition",
]
