"""Deterministic local citation and evidence validation for V1.11 contracts."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from .schema_guard import SchemaGuardError, validate_instance


MAX_SOURCES = 3
MAX_EVIDENCE = 100
MAX_MISSING_FIELDS = 64
MAX_UNCERTAINTIES = 64
MAX_FIELD_CHARS = 256
MAX_QUOTE_CHARS = 500
MAX_DESCRIPTION_CHARS = 300
MAX_UNCERTAINTY_REASON_CHARS = 300
MAX_PAGE = 20

_SOURCE_FIELDS = {"source_id", "title", "url", "retrieval_method", "retrieved_at"}
_EVIDENCE_FIELDS = {
    "field", "source_id", "quote", "description", "page",
    "start_seconds", "end_seconds",
}
_UNCERTAINTY_FIELDS = {"field", "reason"}
_CLAIM_FIELDS = {"text", "citation_ids"}
_CITATION_FIELDS = {"citation_id", "source_id"}


class EvidenceValidationError(ValueError):
    """A finite public-safe result-validation failure."""

    def __init__(self, code: str = "evidence_validation_failed") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StructuredValidationSummary:
    settlement_eligible: bool
    required_non_null_fields: tuple[str, ...]
    disclosed_null_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CitationValidationSummary:
    claim_count: int
    citation_count: int


def _escape_pointer_segment(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str) or len(pointer) > MAX_FIELD_CHARS:
        raise EvidenceValidationError()
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise EvidenceValidationError()
    segments: list[str] = []
    for encoded in pointer[1:].split("/"):
        index = 0
        decoded: list[str] = []
        while index < len(encoded):
            if encoded[index] != "~":
                decoded.append(encoded[index])
                index += 1
                continue
            if index + 1 >= len(encoded) or encoded[index + 1] not in {"0", "1"}:
                raise EvidenceValidationError()
            decoded.append("~" if encoded[index + 1] == "0" else "/")
            index += 2
        segments.append("".join(decoded))
    canonical = "/" + "/".join(_escape_pointer_segment(segment) for segment in segments)
    if canonical != pointer:
        raise EvidenceValidationError()
    return tuple(segments)


def _resolve_pointer(data: Any, pointer: str) -> Any:
    value = data
    for segment in _decode_pointer(pointer):
        if isinstance(value, dict):
            if segment not in value:
                raise EvidenceValidationError()
            value = value[segment]
        elif isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", segment):
                raise EvidenceValidationError()
            index = int(segment)
            if index >= len(value):
                raise EvidenceValidationError()
            value = value[index]
        else:
            raise EvidenceValidationError()
    return value


def _permits_null(schema: Mapping[str, Any]) -> bool:
    schema_type = schema.get("type")
    return schema_type == "null" or (isinstance(schema_type, list) and "null" in schema_type)


def _schema_types(schema: Mapping[str, Any]) -> set[str]:
    schema_type = schema.get("type")
    return set(schema_type) if isinstance(schema_type, list) else {schema_type}


def _collect_fields(
    schema: Mapping[str, Any],
    value: Any,
    pointer: str,
    *,
    required_here: bool,
    required_non_null: list[str],
    null_fields: list[str],
    usable_fields: list[str],
) -> None:
    if value is None:
        if not _permits_null(schema):
            raise EvidenceValidationError("schema_validation_failed")
        null_fields.append(pointer)
        return

    types = _schema_types(schema)
    if "object" in types and isinstance(value, dict):
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        for name, child_schema in properties.items():
            child_pointer = pointer + "/" + _escape_pointer_segment(name)
            if name not in value:
                if name in required:
                    raise EvidenceValidationError("schema_validation_failed")
                continue
            _collect_fields(
                child_schema,
                value[name],
                child_pointer,
                required_here=name in required,
                required_non_null=required_non_null,
                null_fields=null_fields,
                usable_fields=usable_fields,
            )
        return

    if "array" in types and isinstance(value, list):
        item_schema = schema["items"]
        for index, item in enumerate(value):
            _collect_fields(
                item_schema,
                item,
                pointer + "/" + str(index),
                required_here=required_here,
                required_non_null=required_non_null,
                null_fields=null_fields,
                usable_fields=usable_fields,
            )
        return

    usable_fields.append(pointer)
    if required_here:
        required_non_null.append(pointer)


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _bounded_string(value: Any, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and bool(value.strip())
    )


def _finite_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _validate_source_registry(sources: Sequence[Mapping[str, Any]], *, direct: bool) -> dict[str, str]:
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        raise EvidenceValidationError()
    if not 1 <= len(sources) <= MAX_SOURCES or (direct and len(sources) != 1):
        raise EvidenceValidationError()
    registry: dict[str, str] = {}
    allowed_methods = {"http", "browser", "image", "pdf", "audio", "video"}
    for source in sources:
        if not isinstance(source, Mapping) or set(source) - _SOURCE_FIELDS:
            raise EvidenceValidationError()
        source_id = source.get("source_id")
        method = source.get("retrieval_method")
        if (
            not _bounded_string(source_id, 1, 64)
            or not _bounded_string(method, 1, 16)
            or method not in allowed_methods
            or source_id in registry
        ):
            raise EvidenceValidationError()
        registry[source_id] = method
    return registry


def _validate_locator(
    entry: Mapping[str, Any],
    *,
    method: str,
    source_id: str,
    source_texts: Mapping[str, str],
    media_durations: Mapping[str, float],
    page_counts: Mapping[str, int],
) -> None:
    quote = entry.get("quote")
    description = entry.get("description")
    page = entry.get("page")
    start = entry.get("start_seconds")
    end = entry.get("end_seconds")

    if quote is not None and not _bounded_string(quote, 1, MAX_QUOTE_CHARS):
        raise EvidenceValidationError()
    if description is not None and not _bounded_string(description, 1, MAX_DESCRIPTION_CHARS):
        raise EvidenceValidationError()
    if page is not None and (
        not isinstance(page, int)
        or isinstance(page, bool)
        or not 1 <= page <= MAX_PAGE
    ):
        raise EvidenceValidationError()

    if method in {"http", "browser", "pdf"}:
        text = source_texts.get(source_id)
        if not isinstance(text, str) or not isinstance(quote, str):
            raise EvidenceValidationError()
        if _normalized_text(quote) not in _normalized_text(text):
            raise EvidenceValidationError()
        if page is not None:
            page_count = page_counts.get(source_id)
            if (
                not isinstance(page_count, int)
                or isinstance(page_count, bool)
                or page > page_count
            ):
                raise EvidenceValidationError()
        return

    if method == "image":
        if not isinstance(description, str):
            raise EvidenceValidationError()
        return

    duration = media_durations.get(source_id)
    duration_limit = 1800 if method == "audio" else 600
    if (
        not _finite_nonnegative_number(duration)
        or duration > duration_limit
        or not _finite_nonnegative_number(start)
        or not _finite_nonnegative_number(end)
        or end < start
        or end > duration
    ):
        raise EvidenceValidationError()


def validate_structured_result(
    *,
    schema: Mapping[str, Any],
    data: Any,
    sources: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    missing_fields: Sequence[str],
    uncertainties: Sequence[Mapping[str, Any]],
    source_texts: Mapping[str, str] | None = None,
    media_durations: Mapping[str, float] | None = None,
    page_counts: Mapping[str, int] | None = None,
    direct: bool = False,
) -> StructuredValidationSummary:
    """Validate a complete structured result without performing any I/O."""
    try:
        validate_instance(schema, data)
    except SchemaGuardError as exc:
        raise EvidenceValidationError(exc.code) from None

    registry = _validate_source_registry(sources, direct=direct)
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)) or len(evidence) > MAX_EVIDENCE:
        raise EvidenceValidationError()
    if not isinstance(missing_fields, Sequence) or isinstance(missing_fields, (str, bytes)) or len(missing_fields) > MAX_MISSING_FIELDS:
        raise EvidenceValidationError()
    if not isinstance(uncertainties, Sequence) or isinstance(uncertainties, (str, bytes)) or len(uncertainties) > MAX_UNCERTAINTIES:
        raise EvidenceValidationError()

    required_non_null: list[str] = []
    null_fields: list[str] = []
    usable_fields: list[str] = []
    _collect_fields(
        schema,
        data,
        "",
        required_here=False,
        required_non_null=required_non_null,
        null_fields=null_fields,
        usable_fields=usable_fields,
    )
    if not usable_fields:
        raise EvidenceValidationError()

    missing = tuple(missing_fields)
    if len(set(missing)) != len(missing):
        raise EvidenceValidationError()
    for pointer in missing:
        _decode_pointer(pointer)
        if _resolve_pointer(data, pointer) is not None:
            raise EvidenceValidationError()
    if set(missing) != set(null_fields):
        raise EvidenceValidationError()

    uncertainty_fields: list[str] = []
    for uncertainty in uncertainties:
        if not isinstance(uncertainty, Mapping) or set(uncertainty) - _UNCERTAINTY_FIELDS:
            raise EvidenceValidationError()
        field = uncertainty.get("field")
        reason = uncertainty.get("reason")
        _decode_pointer(field)
        if not _bounded_string(reason, 1, MAX_UNCERTAINTY_REASON_CHARS):
            raise EvidenceValidationError()
        uncertainty_fields.append(field)
    if len(set(uncertainty_fields)) != len(uncertainty_fields) or set(uncertainty_fields) != set(null_fields):
        raise EvidenceValidationError()

    source_texts = source_texts or {}
    media_durations = media_durations or {}
    page_counts = page_counts or {}
    evidenced_fields: set[str] = set()
    for entry in evidence:
        if not isinstance(entry, Mapping) or set(entry) - _EVIDENCE_FIELDS:
            raise EvidenceValidationError()
        field = entry.get("field")
        source_id = entry.get("source_id")
        if not _bounded_string(source_id, 1, 64) or source_id not in registry:
            raise EvidenceValidationError()
        value = _resolve_pointer(data, field)
        if value is None or field in missing:
            raise EvidenceValidationError()
        _validate_locator(
            entry,
            method=registry[source_id],
            source_id=source_id,
            source_texts=source_texts,
            media_durations=media_durations,
            page_counts=page_counts,
        )
        evidenced_fields.add(field)

    if any(pointer not in evidenced_fields for pointer in required_non_null):
        raise EvidenceValidationError()
    return StructuredValidationSummary(
        settlement_eligible=True,
        required_non_null_fields=tuple(sorted(required_non_null)),
        disclosed_null_fields=tuple(sorted(null_fields)),
    )


def validate_cited_answer(
    *,
    answer: str,
    claims: Sequence[Mapping[str, Any]],
    citations: Sequence[Mapping[str, Any]],
    retrieved_source_ids: Iterable[str],
) -> CitationValidationSummary:
    """Validate bounded claims and opaque citation/source relationships."""
    if (
        not _bounded_string(answer, 1, 12000)
        or not isinstance(claims, Sequence)
        or isinstance(claims, (str, bytes))
        or not isinstance(citations, Sequence)
        or isinstance(citations, (str, bytes))
        or not 1 <= len(claims) <= 50
        or not 1 <= len(citations) <= 20
    ):
        raise EvidenceValidationError("invalid_provider_output")
    if isinstance(retrieved_source_ids, (str, bytes)):
        raise EvidenceValidationError("invalid_provider_output")
    allowed_sources: set[str] = set()
    try:
        for source_id in retrieved_source_ids:
            if not _bounded_string(source_id, 1, 64):
                raise EvidenceValidationError("invalid_provider_output")
            allowed_sources.add(source_id)
    except TypeError:
        raise EvidenceValidationError("invalid_provider_output") from None
    citation_registry: dict[str, str] = {}
    for citation in citations:
        if not isinstance(citation, Mapping) or set(citation) - _CITATION_FIELDS:
            raise EvidenceValidationError("invalid_provider_output")
        citation_id = citation.get("citation_id")
        source_id = citation.get("source_id")
        if (
            not _bounded_string(citation_id, 1, 64)
            or not _bounded_string(source_id, 1, 64)
            or citation_id in citation_registry
            or source_id not in allowed_sources
        ):
            raise EvidenceValidationError("invalid_provider_output")
        citation_registry[citation_id] = source_id
    for claim in claims:
        if (
            not isinstance(claim, Mapping)
            or set(claim) - _CLAIM_FIELDS
            or not _bounded_string(claim.get("text"), 1, 500)
        ):
            raise EvidenceValidationError("invalid_provider_output")
        ids = claim.get("citation_ids")
        if (
            not isinstance(ids, Sequence)
            or isinstance(ids, (str, bytes))
            or not 1 <= len(ids) <= 20
            or any(
                not _bounded_string(item, 1, 64) or item not in citation_registry
                for item in ids
            )
        ):
            raise EvidenceValidationError("invalid_provider_output")
    return CitationValidationSummary(len(claims), len(citations))


__all__ = [
    "CitationValidationSummary",
    "EvidenceValidationError",
    "StructuredValidationSummary",
    "validate_cited_answer",
    "validate_structured_result",
]
