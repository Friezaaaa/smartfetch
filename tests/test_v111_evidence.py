import unittest

from smartfetch.evidence import (
    EvidenceValidationError,
    validate_cited_answer,
    validate_structured_result,
)


SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "string"},
        "release_date": {"type": ["string", "null"]},
    },
    "required": ["version", "release_date"],
    "additionalProperties": False,
}
SOURCE = {
    "source_id": "s1",
    "retrieval_method": "http",
    "title": "Release",
    "url": "https://example.com/release",
}


class NullableAndRequiredLeafTests(unittest.TestCase):
    def test_nullable_missing_field_with_usable_evidenced_data_succeeds(self):
        result = validate_structured_result(
            schema=SCHEMA,
            data={"version": "2.20.0", "release_date": None},
            sources=[SOURCE],
            evidence=[{
                "field": "/version",
                "source_id": "s1",
                "quote": "x402 Python 2.20.0",
            }],
            missing_fields=["/release_date"],
            uncertainties=[{
                "field": "/release_date",
                "reason": "No release date was present.",
            }],
            source_texts={"s1": "Official release:   x402 Python 2.20.0 is stable."},
        )
        self.assertTrue(result.settlement_eligible)
        self.assertEqual(result.required_non_null_fields, ("/version",))

    def test_missing_required_nonnullable_field_fails(self):
        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema=SCHEMA,
                data={"release_date": None},
                sources=[SOURCE],
                evidence=[],
                missing_fields=["/release_date"],
                uncertainties=[{"field": "/release_date", "reason": "Absent."}],
                source_texts={"s1": "nothing"},
            )

    def test_null_requires_missing_field_and_uncertainty(self):
        cases = [
            ([], [{"field": "/release_date", "reason": "Absent."}]),
            (["/release_date"], []),
        ]
        for missing_fields, uncertainties in cases:
            with self.subTest(missing_fields=missing_fields), self.assertRaises(EvidenceValidationError):
                validate_structured_result(
                    schema=SCHEMA,
                    data={"version": "2.20.0", "release_date": None},
                    sources=[SOURCE],
                    evidence=[{"field": "/version", "source_id": "s1", "quote": "2.20.0"}],
                    missing_fields=missing_fields,
                    uncertainties=uncertainties,
                    source_texts={"s1": "Version 2.20.0"},
                )

    def test_non_null_required_leaf_without_evidence_fails(self):
        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema=SCHEMA,
                data={"version": "2.20.0", "release_date": None},
                sources=[SOURCE],
                evidence=[],
                missing_fields=["/release_date"],
                uncertainties=[{"field": "/release_date", "reason": "Absent."}],
                source_texts={"s1": "Version 2.20.0"},
            )

    def test_all_requested_values_null_or_missing_fails(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": ["string", "null"]}},
            "required": ["value"],
            "additionalProperties": False,
        }
        with self.assertRaises(EvidenceValidationError) as caught:
            validate_structured_result(
                schema=schema,
                data={"value": None},
                sources=[SOURCE],
                evidence=[],
                missing_fields=["/value"],
                uncertainties=[{"field": "/value", "reason": "Absent."}],
                source_texts={"s1": "nothing"},
            )
        self.assertEqual(caught.exception.code, "evidence_validation_failed")

    def test_nested_array_and_escaped_property_pointers_are_canonical(self):
        schema = {
            "type": "object",
            "properties": {
                "a/b": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"~name": {"type": "string"}},
                        "required": ["~name"],
                        "additionalProperties": False,
                    },
                    "maxItems": 2,
                },
            },
            "required": ["a/b"],
            "additionalProperties": False,
        }
        result = validate_structured_result(
            schema=schema,
            data={"a/b": [{"~name": "Ada"}]},
            sources=[SOURCE],
            evidence=[{"field": "/a~1b/0/~0name", "source_id": "s1", "quote": "Ada"}],
            missing_fields=[],
            uncertainties=[],
            source_texts={"s1": "Author: Ada"},
        )
        self.assertEqual(result.required_non_null_fields, ("/a~1b/0/~0name",))


class EvidenceLocatorTests(unittest.TestCase):
    def test_text_quote_must_resolve_to_returned_source_content(self):
        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                data={"value": "claimed"},
                sources=[SOURCE],
                evidence=[{"field": "/value", "source_id": "s1", "quote": "not present"}],
                missing_fields=[],
                uncertainties=[],
                source_texts={"s1": "bounded source text"},
            )

    def test_whitespace_only_text_evidence_is_not_a_positive_citation(self):
        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                data={"value": "claimed"},
                sources=[SOURCE],
                evidence=[{"field": "/value", "source_id": "s1", "quote": "   "}],
                missing_fields=[],
                uncertainties=[],
                source_texts={"s1": "bounded source text"},
            )

    def test_image_description_and_media_time_ranges_are_bounded(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        image_source = {**SOURCE, "retrieval_method": "image"}
        audio_source = {**SOURCE, "retrieval_method": "audio"}
        validate_structured_result(
            schema=schema,
            data={"value": "blue"},
            sources=[image_source],
            evidence=[{"field": "/value", "source_id": "s1", "description": "Blue chart bar"}],
            missing_fields=[],
            uncertainties=[],
            direct=True,
        )
        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema=schema,
                data={"value": "spoken"},
                sources=[audio_source],
                evidence=[{"field": "/value", "source_id": "s1", "start_seconds": 8, "end_seconds": 12}],
                missing_fields=[],
                uncertainties=[],
                media_durations={"s1": 10},
                direct=True,
            )

    def test_source_and_entry_bounds_fail_closed(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema=schema,
                data={"value": "ok"},
                sources=[SOURCE, {**SOURCE, "source_id": "s2"}],
                evidence=[{"field": "/value", "source_id": "s1", "quote": "ok"}],
                missing_fields=[],
                uncertainties=[],
                source_texts={"s1": "ok"},
                direct=True,
            )

    def test_page_locator_has_an_absolute_bound(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        pdf_source = {**SOURCE, "retrieval_method": "pdf"}
        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema=schema,
                data={"value": "total"},
                sources=[pdf_source],
                evidence=[{
                    "field": "/value",
                    "source_id": "s1",
                    "quote": "total",
                    "page": 21,
                }],
                missing_fields=[],
                uncertainties=[],
                source_texts={"s1": "total"},
                page_counts={"s1": 21},
                direct=True,
            )

    def test_hostile_numeric_and_container_shapes_fail_with_finite_error(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        audio_source = {**SOURCE, "retrieval_method": "audio"}
        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema=schema,
                data={"value": "spoken"},
                sources=[audio_source],
                evidence=[{"field": "/value", "source_id": "s1", "start_seconds": 0, "end_seconds": 1}],
                missing_fields=[],
                uncertainties=[],
                media_durations={"s1": 10**10000},
                direct=True,
            )

        malformed_cases = [
            {
                "sources": [{**SOURCE, "retrieval_method": ["http"]}],
                "evidence": [],
            },
            {
                "sources": [SOURCE],
                "evidence": [{"field": "/value", "source_id": ["s1"], "quote": "spoken"}],
            },
        ]
        for case in malformed_cases:
            with self.subTest(case=case), self.assertRaises(EvidenceValidationError):
                validate_structured_result(
                    schema=schema,
                    data={"value": "spoken"},
                    sources=case["sources"],
                    evidence=case["evidence"],
                    missing_fields=[],
                    uncertainties=[],
                    source_texts={"s1": "spoken"},
                    direct=True,
                )


class CitationValidationTests(unittest.TestCase):
    def test_empty_answer_evidence_is_not_delivery_eligible(self):
        with self.assertRaises(EvidenceValidationError):
            validate_cited_answer(
                answer="unsupported answer",
                claims=[],
                citations=[],
                retrieved_source_ids={"s1"},
            )

    def test_claims_must_reference_returned_retrieved_sources(self):
        summary = validate_cited_answer(
            answer="The wrapper protects execution.",
            claims=[{"text": "The wrapper protects execution.", "citation_ids": ["c1"]}],
            citations=[{"citation_id": "c1", "source_id": "s1"}],
            retrieved_source_ids={"s1"},
        )
        self.assertEqual(summary.claim_count, 1)
        for bad_claims, bad_citations in [
            ([{"text": "claim", "citation_ids": []}], [{"citation_id": "c1", "source_id": "s1"}]),
            ([{"text": "claim", "citation_ids": ["missing"]}], [{"citation_id": "c1", "source_id": "s1"}]),
            ([{"text": "claim", "citation_ids": ["c1"]}], [{"citation_id": "c1", "source_id": "s2"}]),
        ]:
            with self.subTest(claims=bad_claims), self.assertRaises(EvidenceValidationError):
                validate_cited_answer(
                    answer="answer",
                    claims=bad_claims,
                    citations=bad_citations,
                    retrieved_source_ids={"s1"},
                )

    def test_malformed_provider_containers_fail_with_finite_error(self):
        with self.assertRaises(EvidenceValidationError):
            validate_cited_answer(
                answer="answer",
                claims=None,
                citations=[],
                retrieved_source_ids={"s1"},
            )

        malformed = [
            {
                "claims": [{"text": "claim", "citation_ids": [["c1"]]}],
                "citations": [{"citation_id": "c1", "source_id": "s1"}],
                "retrieved_source_ids": {"s1"},
            },
            {
                "claims": [{"text": "claim", "citation_ids": ["c1"]}],
                "citations": [{"citation_id": "c1", "source_id": "s1"}],
                "retrieved_source_ids": [["s1"]],
            },
        ]
        for case in malformed:
            with self.subTest(case=case), self.assertRaises(EvidenceValidationError):
                validate_cited_answer(answer="answer", **case)


if __name__ == "__main__":
    unittest.main()
