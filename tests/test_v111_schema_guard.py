import json
import unittest
from unittest.mock import patch

from smartfetch.schema_guard import (
    SchemaGuardError,
    validate_instance,
    validate_schema,
)


def object_schema(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


class SchemaSubsetTests(unittest.TestCase):
    def test_accepts_bounded_nested_schema_and_nullable_type(self):
        schema = object_schema({
            "name": {"type": "string", "maxLength": 100},
            "release_date": {"type": ["string", "null"]},
            "items": {
                "type": "array",
                "maxItems": 10,
                "items": object_schema({"amount": {"type": "number"}}, ["amount"]),
            },
        }, ["name", "release_date", "items"])
        validate_schema(schema)

    def test_rejects_every_non_allowlisted_keyword_and_reference_construct(self):
        keywords = [
            "$ref", "$defs", "definitions", "allOf", "anyOf", "oneOf",
            "not", "if", "then", "else", "pattern", "patternProperties",
            "format", "dependencies", "dependentSchemas", "unevaluatedProperties",
            "x-execute",
        ]
        for keyword in keywords:
            schema = object_schema({"value": {"type": "string", keyword: "canary"}})
            with self.subTest(keyword=keyword), self.assertRaises(SchemaGuardError):
                validate_schema(schema)

    def test_rejects_wrong_root_recursive_shapes_and_missing_array_items(self):
        invalid = [
            {"type": "string"},
            {"type": "object", "properties": {}, "additionalProperties": True},
            object_schema({"items": {"type": "array"}}),
            object_schema({"value": {"type": ["string", "number"]}}),
            object_schema({"value": {"type": ["string", "null", "number"]}}),
            object_schema({"value": {"type": "made-up"}}),
        ]
        for schema in invalid:
            with self.subTest(schema=schema), self.assertRaises(SchemaGuardError):
                validate_schema(schema)

    def test_rejects_depth_property_required_and_name_limits(self):
        nested = {"type": "string"}
        for index in range(6):
            nested = object_schema({f"level{index}": nested}, [f"level{index}"])
        too_many_properties = object_schema({f"p{i}": {"type": "string"} for i in range(65)})
        undeclared_required = object_schema({"known": {"type": "string"}}, ["missing"])
        duplicate_required = object_schema({"known": {"type": "string"}}, ["known", "known"])
        long_name = object_schema({"x" * 129: {"type": "string"}})
        for schema in [nested, too_many_properties, undeclared_required, duplicate_required, long_name]:
            with self.subTest(schema=schema), self.assertRaises(SchemaGuardError):
                validate_schema(schema)

    def test_rejects_schema_enum_and_description_byte_limits(self):
        too_large = object_schema({
            "value": {"type": "string", "description": "x" * 17000},
        })
        too_many_enum_values = object_schema({
            "value": {"type": "integer", "enum": list(range(129))},
        })
        oversized_enum_value = object_schema({
            "value": {"type": "string", "enum": ["x" * 257]},
        })
        long_description = object_schema({
            "value": {"type": "string", "description": "x" * 501},
        })
        for schema in [too_large, too_many_enum_values, oversized_enum_value, long_description]:
            with self.subTest(size=len(json.dumps(schema))), self.assertRaises(SchemaGuardError):
                validate_schema(schema)

    def test_rejects_invalid_array_and_string_bounds_and_non_finite_numbers(self):
        invalid = [
            object_schema({"items": {"type": "array", "items": {"type": "string"}, "maxItems": 101}}),
            object_schema({"items": {"type": "array", "items": {"type": "string"}, "minItems": 5, "maxItems": 4}}),
            object_schema({"value": {"type": "string", "minLength": 5, "maxLength": 4}}),
            object_schema({"value": {"type": "number", "minimum": float("nan")}}),
            object_schema({"value": {"type": "number", "maximum": float("inf")}}),
            object_schema({"value": {"type": ["string", {}]}}),
            object_schema({"items": {"type": "array", "items": {"type": "string"}, "minItems": 101}}),
        ]
        for schema in invalid:
            with self.subTest(schema=schema), self.assertRaises(SchemaGuardError):
                validate_schema(schema)

    def test_rejects_keywords_that_do_not_apply_to_the_declared_type(self):
        invalid = [
            object_schema({"value": {"type": "string", "minimum": 0}}),
            object_schema({"value": {"type": "number", "minLength": 1}}),
            object_schema({"value": {"type": "integer", "maxLength": 3}}),
            object_schema({"value": {"type": "boolean", "maximum": 1}}),
            object_schema({"value": {"type": "array", "items": {"type": "string"}, "maxLength": 3}}),
            object_schema({"value": {"type": "object", "properties": {}, "additionalProperties": False, "minimum": 0}}),
            object_schema({"value": {"type": ["string", "null"], "minimum": 0}}),
            object_schema({"value": {"type": ["number", "null"], "maxLength": 3}}),
        ]
        for schema in invalid:
            with self.subTest(schema=schema), self.assertRaises(SchemaGuardError):
                validate_schema(schema)

        validate_schema(object_schema({
            "text": {"type": ["string", "null"], "minLength": 0, "maxLength": 3},
            "count": {"type": ["integer", "null"], "minimum": 0, "maximum": 3},
        }))

    def test_pathological_plain_data_fails_with_finite_error(self):
        deep = {"type": "string"}
        for _ in range(2000):
            deep = {"type": "array", "items": deep}
        invalid_values = [
            object_schema({"value": {"type": "string", "const": "\ud800"}}),
            deep,
            object_schema({"value": {"type": "number", "minimum": 10**10000}}),
        ]
        for value in invalid_values:
            with self.subTest(kind=type(value).__name__), self.assertRaises(SchemaGuardError):
                validate_schema(value)

    def test_schema_walk_does_not_evaluate_import_or_resolve_network(self):
        schema = object_schema({
            "value": {
                "type": "string",
                "const": "__import__('os').system('CANARY')",
                "description": "{{ dangerous_template }}",
            },
        })
        with (
            patch("builtins.eval", side_effect=AssertionError("eval called")),
            patch("builtins.__import__", side_effect=AssertionError("import called")),
            patch("socket.getaddrinfo", side_effect=AssertionError("network called")),
        ):
            validate_schema(schema)


class LocalInstanceValidationTests(unittest.TestCase):
    def test_validates_with_jsonschema_after_subset_guard(self):
        schema = object_schema({"count": {"type": "integer", "minimum": 1}}, ["count"])
        validate_instance(schema, {"count": 1})
        with self.assertRaises(SchemaGuardError) as caught:
            validate_instance(schema, {"count": 0})
        self.assertEqual(caught.exception.code, "schema_validation_failed")

    def test_rejects_serialized_output_larger_than_64_kib(self):
        schema = object_schema({"value": {"type": "string"}}, ["value"])
        with self.assertRaises(SchemaGuardError) as caught:
            validate_instance(schema, {"value": "x" * (64 * 1024)})
        self.assertEqual(caught.exception.code, "schema_validation_failed")

    def test_failures_are_finite_and_do_not_echo_schema_or_data(self):
        canary = "SCHEMA_SECRET_CANARY"
        schema = object_schema({canary: {"type": "string"}}, [canary])
        with self.assertRaises(SchemaGuardError) as caught:
            validate_instance(schema, {canary: 123})
        self.assertNotIn(canary, str(caught.exception))
        self.assertIn(caught.exception.code, {"invalid_schema", "schema_validation_failed"})


if __name__ == "__main__":
    unittest.main()
