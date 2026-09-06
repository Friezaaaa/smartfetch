"""Fail-closed validator for SmartFetch's deliberately small JSON Schema subset."""

from __future__ import annotations

import json
import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


MAX_SCHEMA_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_DEPTH = 6
MAX_PROPERTIES = 64
MAX_REQUIRED = 64
MAX_ARRAY_ITEMS = 100
MAX_ENUM_VALUES = 128
MAX_ENUM_VALUE_BYTES = 256
MAX_PROPERTY_NAME_CHARS = 128
MAX_DESCRIPTION_CHARS = 500

_ALLOWED_KEYWORDS = {
    "$schema", "title", "description", "type", "properties", "required",
    "additionalProperties", "items", "minItems", "maxItems", "enum", "const",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength",
}
_ALLOWED_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_NUMBER_KEYWORDS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}


class SchemaGuardError(ValueError):
    """A finite public-safe schema or output validation failure."""

    def __init__(self, code: str = "invalid_schema") -> None:
        self.code = code
        super().__init__(code)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise SchemaGuardError() from None


def _bounded_nonnegative_integer(value: Any, *, upper: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        and (upper is None or value <= upper)
    )


def _walk_schema(schema: Any, *, depth: int, counters: dict[str, int]) -> None:
    if not isinstance(schema, dict) or depth > MAX_DEPTH:
        raise SchemaGuardError()
    if set(schema) - _ALLOWED_KEYWORDS:
        raise SchemaGuardError()

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if (
            len(schema_type) != 2
            or any(not isinstance(item, str) for item in schema_type)
            or len(set(schema_type)) != 2
            or "null" not in schema_type
            or any(item not in _ALLOWED_TYPES for item in schema_type)
        ):
            raise SchemaGuardError()
        effective_types = set(schema_type)
    elif isinstance(schema_type, str) and schema_type in _ALLOWED_TYPES:
        effective_types = {schema_type}
    else:
        raise SchemaGuardError()

    for text_key in ("title", "description", "$schema"):
        if text_key in schema and not isinstance(schema[text_key], str):
            raise SchemaGuardError()
    if len(schema.get("description", "")) > MAX_DESCRIPTION_CHARS:
        raise SchemaGuardError()

    if "string" not in effective_types and ({"minLength", "maxLength"} & set(schema)):
        raise SchemaGuardError()
    if not ({"number", "integer"} & effective_types) and (_NUMBER_KEYWORDS & set(schema)):
        raise SchemaGuardError()

    for keyword in _NUMBER_KEYWORDS:
        if keyword in schema:
            value = schema[keyword]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise SchemaGuardError()

    for minimum_key, maximum_key in (("minLength", "maxLength"), ("minItems", "maxItems")):
        minimum_upper = MAX_ARRAY_ITEMS if minimum_key == "minItems" else None
        if minimum_key in schema and not _bounded_nonnegative_integer(
            schema[minimum_key],
            upper=minimum_upper,
        ):
            raise SchemaGuardError()
        upper = MAX_ARRAY_ITEMS if maximum_key in {"minItems", "maxItems"} else None
        if maximum_key in schema and not _bounded_nonnegative_integer(schema[maximum_key], upper=upper):
            raise SchemaGuardError()
        if (
            minimum_key in schema
            and maximum_key in schema
            and schema[minimum_key] > schema[maximum_key]
        ):
            raise SchemaGuardError()

    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            raise SchemaGuardError()
        counters["enum"] += len(values)
        if counters["enum"] > MAX_ENUM_VALUES:
            raise SchemaGuardError()
        if any(len(_canonical_json_bytes(value)) > MAX_ENUM_VALUE_BYTES for value in values):
            raise SchemaGuardError()

    if "object" in effective_types:
        properties = schema.get("properties")
        if not isinstance(properties, dict) or schema.get("additionalProperties") is not False:
            raise SchemaGuardError()
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or len(required) > MAX_REQUIRED
            or any(not isinstance(name, str) for name in required)
            or len(set(required)) != len(required)
            or any(name not in properties for name in required)
        ):
            raise SchemaGuardError()
        counters["properties"] += len(properties)
        if counters["properties"] > MAX_PROPERTIES:
            raise SchemaGuardError()
        for name, child in properties.items():
            if not isinstance(name, str) or len(name) > MAX_PROPERTY_NAME_CHARS:
                raise SchemaGuardError()
            _walk_schema(child, depth=depth + 1, counters=counters)
    elif "properties" in schema or "required" in schema or "additionalProperties" in schema:
        raise SchemaGuardError()

    if "array" in effective_types:
        if "items" not in schema:
            raise SchemaGuardError()
        _walk_schema(schema["items"], depth=depth + 1, counters=counters)
    elif "items" in schema or "minItems" in schema or "maxItems" in schema:
        raise SchemaGuardError()


def validate_schema(schema: Any) -> None:
    """Validate the bounded subset without imports, evaluation, or I/O."""
    if len(_canonical_json_bytes(schema)) > MAX_SCHEMA_BYTES:
        raise SchemaGuardError("schema_too_large")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise SchemaGuardError()
    _walk_schema(schema, depth=1, counters={"properties": 0, "enum": 0})
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise SchemaGuardError() from None


def validate_instance(schema: Any, data: Any) -> None:
    """Guard the schema, bound output bytes, then validate locally."""
    validate_schema(schema)
    try:
        output = _canonical_json_bytes(data)
    except SchemaGuardError:
        raise SchemaGuardError("schema_validation_failed") from None
    if len(output) > MAX_OUTPUT_BYTES:
        raise SchemaGuardError("schema_validation_failed")
    try:
        Draft202012Validator(schema).validate(data)
    except ValidationError:
        raise SchemaGuardError("schema_validation_failed") from None


__all__ = [
    "MAX_OUTPUT_BYTES",
    "MAX_SCHEMA_BYTES",
    "SchemaGuardError",
    "validate_instance",
    "validate_schema",
]
