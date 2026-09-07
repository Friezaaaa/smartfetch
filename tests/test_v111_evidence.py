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

    def test_rfc_6901_root_pointer_is_valid_but_cannot_replace_leaf_evidence(self):
        root_result = validate_structured_result(
            schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "additionalProperties": False,
            },
            data={"value": "whole value"},
            sources=[SOURCE],
            evidence=[{"field": "", "source_id": "s1", "quote": "whole value"}],
            missing_fields=[],
            uncertainties=[],
            source_texts={"s1": "The whole value is present."},
        )
        self.assertTrue(root_result.settlement_eligible)

        with self.assertRaises(EvidenceValidationError):
            validate_structured_result(
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                data={"value": "whole value"},
                sources=[SOURCE],
                evidence=[{"field": "", "source_id": "s1", "quote": "whole value"}],
                missing_fields=[],
                uncertainties=[],
                source_texts={"s1": "The whole value is present."},
            )

    def test_missing_field_pointers_are_validated_before_duplicate_detection(self):
        malformed = (
            [[]],
            [{}],
            [True],
            [1],
            [None],
            [""],
            ["/bad~2escape"],
            ["/" + ("x" * 256)],
        )
        for missing_fields in malformed:
            with self.subTest(value_type=type(missing_fields[0]).__name__):
                with self.assertRaisesRegex(
                    EvidenceValidationError,
                    "^evidence_validation_failed$",
                ) as caught:
                    validate_structured_result(
                        schema=SCHEMA,
                        data={"version": "2.20.0", "release_date": None},
                        sources=[SOURCE],
                        evidence=[{"field": "/version", "source_id": "s1", "quote": "2.20.0"}],
                        missing_fields=missing_fields,
                        uncertainties=[{"field": "/release_date", "reason": "Absent."}],
                        source_texts={"s1": "Version 2.20.0"},
                    )
                self.assertEqual(str(caught.exception), "evidence_validation_failed")

        with self.assertRaisesRegex(EvidenceValidationError, "^evidence_validation_failed$"):
            validate_structured_result(
                schema=SCHEMA,
                data={"version": "2.20.0", "release_date": None},
                sources=[SOURCE],
                evidence=[{"field": "/version", "source_id": "s1", "quote": "2.20.0"}],
                missing_fields=["/release_date", "/release_date"],
                uncertainties=[{"field": "/release_date", "reason": "Absent."}],
                source_texts={"s1": "Version 2.20.0"},
            )


class EvidenceLocatorTests(unittest.TestCase):
    def test_structured_provider_objects_reject_unknown_keys_without_echoing_canaries(self):
        cases = [
            {
                "sources": [{**SOURCE, "raw_content": "SOURCE_SECRET_CANARY"}],
                "evidence": [{"field": "/version", "source_id": "s1", "quote": "2.20.0"}],
                "missing_fields": ["/release_date"],
                "uncertainties": [{"field": "/release_date", "reason": "Absent."}],
            },
            {
                "sources": [SOURCE],
                "evidence": [{
                    "field": "/version",
                    "source_id": "s1",
                    "quote": "2.20.0",
                    "provider_payload": {"raw": "EVIDENCE_SECRET_CANARY"},
                }],
                "missing_fields": ["/release_date"],
                "uncertainties": [{"field": "/release_date", "reason": "Absent."}],
            },
            {
                "sources": [SOURCE],
                "evidence": [{"field": "/version", "source_id": "s1", "quote": "2.20.0"}],
                "missing_fields": ["/release_date"],
                "uncertainties": [{
                    "field": "/release_date",
                    "reason": "Absent.",
                    "raw": {"nested": "UNCERTAINTY_SECRET_CANARY"},
                }],
            },
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(EvidenceValidationError) as caught:
                validate_structured_result(
                    schema=SCHEMA,
                    data={"version": "2.20.0", "release_date": None},
                    source_texts={"s1": "Version 2.20.0"},
                    **case,
                )
            self.assertEqual(str(caught.exception), "evidence_validation_failed")
            self.assertNotIn("CANARY", str(caught.exception))

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

    def test_pdf_page_count_rejects_booleans(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        pdf_source = {**SOURCE, "retrieval_method": "pdf"}
        valid = validate_structured_result(
            schema=schema,
            data={"value": "total"},
            sources=[pdf_source],
            evidence=[{
                "field": "/value",
                "source_id": "s1",
                "quote": "total",
                "page": 1,
            }],
            missing_fields=[],
            uncertainties=[],
            source_texts={"s1": "total"},
            page_counts={"s1": 1},
            direct=True,
        )
        self.assertTrue(valid.settlement_eligible)
        for page_count in (True, False):
            with self.subTest(page_count=page_count), self.assertRaises(EvidenceValidationError):
                validate_structured_result(
                    schema=schema,
                    data={"value": "total"},
                    sources=[pdf_source],
                    evidence=[{
                        "field": "/value",
                        "source_id": "s1",
                        "quote": "total",
                        "page": 1,
                    }],
                    missing_fields=[],
                    uncertainties=[],
                    source_texts={"s1": "total"},
                    page_counts={"s1": page_count},
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
    def test_claims_and_citations_reject_unknown_keys_without_echoing_canaries(self):
        cases = [
            (
                [{
                    "text": "claim",
                    "citation_ids": ["c1"],
                    "raw": {"nested": "CLAIM_SECRET_CANARY"},
                }],
                [{"citation_id": "c1", "source_id": "s1"}],
            ),
            (
                [{"text": "claim", "citation_ids": ["c1"]}],
                [{
                    "citation_id": "c1",
                    "source_id": "s1",
                    "provider_object": {"raw": "CITATION_SECRET_CANARY"},
                }],
            ),
        ]
        for claims, citations in cases:
            with self.subTest(claims=claims), self.assertRaises(EvidenceValidationError) as caught:
                validate_cited_answer(
                    answer="answer",
                    claims=claims,
                    citations=citations,
                    retrieved_source_ids={"s1"},
                )
            self.assertEqual(str(caught.exception), "invalid_provider_output")
            self.assertNotIn("CANARY", str(caught.exception))

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


class EvidencePlainBoundaryTests(unittest.TestCase):
    @staticmethod
    def _valid_structured_kwargs():
        return {
            "schema": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "optional": {"type": ["string", "null"]},
                },
                "required": ["value", "optional"],
                "additionalProperties": False,
            },
            "data": {"value": "ok", "optional": None},
            "sources": [SOURCE],
            "evidence": [{"field": "/value", "source_id": "s1", "quote": "ok"}],
            "missing_fields": ["/optional"],
            "uncertainties": [{"field": "/optional", "reason": "Absent."}],
            "source_texts": {"s1": "ok"},
        }

    def test_pointer_values_are_exact_owned_strings_before_hashing(self):
        class UnhashableText(str):
            __hash__ = None

        class HostileText(str):
            def __len__(self):
                raise RuntimeError("LENGTH_CANARY")

            def __hash__(self):
                raise RuntimeError("HASH_CANARY")

            def __eq__(self, other):
                raise RuntimeError("EQUALITY_CANARY")

        cases = (
            {"missing_fields": [UnhashableText("/optional")]},
            {"missing_fields": [HostileText("/optional")]},
            {"uncertainties": [{"field": UnhashableText("/optional"), "reason": "Absent."}]},
            {"evidence": [{"field": HostileText("/value"), "source_id": "s1", "quote": "ok"}]},
        )
        for replacement in cases:
            kwargs = self._valid_structured_kwargs()
            kwargs.update(replacement)
            with self.subTest(field=next(iter(replacement))):
                with self.assertRaisesRegex(EvidenceValidationError, "^evidence_validation_failed$") as caught:
                    validate_structured_result(**kwargs)
                self.assertNotIn("CANARY", str(caught.exception))

    def test_outer_provider_containers_are_exact_before_iteration_or_length(self):
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

            def __len__(self):
                raise RuntimeError("LENGTH_CANARY")

        cases = (
            {"sources": HostileList([SOURCE])},
            {"evidence": HostileTuple(())},
            {"missing_fields": HostileList(["/optional"])},
            {"uncertainties": HostileTuple(({"field": "/optional", "reason": "Absent."},))},
            {"sources": [HostileDict(SOURCE)]},
        )
        for replacement in cases:
            kwargs = self._valid_structured_kwargs()
            kwargs.update(replacement)
            with self.subTest(field=next(iter(replacement))):
                with self.assertRaises(EvidenceValidationError) as caught:
                    validate_structured_result(**kwargs)
                self.assertNotIn("CANARY", str(caught.exception))

    def test_numeric_subclasses_are_rejected_before_comparison(self):
        class HostileInteger(int):
            def __ge__(self, other):
                raise RuntimeError("COMPARE_CANARY")

            def __le__(self, other):
                raise RuntimeError("COMPARE_CANARY")

        class HostileFloat(float):
            def __ge__(self, other):
                raise RuntimeError("COMPARE_CANARY")

            def __le__(self, other):
                raise RuntimeError("COMPARE_CANARY")

        pdf_kwargs = self._valid_structured_kwargs()
        pdf_kwargs["sources"] = [{**SOURCE, "retrieval_method": "pdf"}]
        pdf_kwargs["evidence"] = [{
            "field": "/value", "source_id": "s1", "quote": "ok", "page": HostileInteger(1),
        }]
        pdf_kwargs["page_counts"] = {"s1": 1}
        audio_kwargs = self._valid_structured_kwargs()
        audio_kwargs["sources"] = [{**SOURCE, "retrieval_method": "audio"}]
        audio_kwargs["evidence"] = [{
            "field": "/value", "source_id": "s1",
            "start_seconds": HostileFloat(0), "end_seconds": 1.0,
        }]
        audio_kwargs["media_durations"] = {"s1": 10.0}
        for kwargs in (pdf_kwargs, audio_kwargs):
            with self.subTest(method=kwargs["sources"][0]["retrieval_method"]):
                with self.assertRaises(EvidenceValidationError) as caught:
                    validate_structured_result(**kwargs)
                self.assertNotIn("CANARY", str(caught.exception))

    def test_valid_and_duplicate_canonical_pointers_keep_existing_semantics(self):
        kwargs = self._valid_structured_kwargs()
        summary = validate_structured_result(**kwargs)
        self.assertTrue(summary.settlement_eligible)
        self.assertEqual(summary.disclosed_null_fields, ("/optional",))

        kwargs = self._valid_structured_kwargs()
        kwargs["missing_fields"] = ["/optional", "/optional"]
        with self.assertRaisesRegex(EvidenceValidationError, "^evidence_validation_failed$"):
            validate_structured_result(**kwargs)


if __name__ == "__main__":
    unittest.main()
