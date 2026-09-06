import unittest

from pydantic import ValidationError

from smartfetch.v111_contracts import (
    DirectExtractionRequest,
    EvidenceEntry,
    FailureResponse,
    SearchAnswerResponse,
    SearchAndExtractRequest,
    SearchResultsResponse,
    StructuredResponse,
    V111_VARIANTS,
)


MINIMAL_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}


class VariantDefinitionTests(unittest.TestCase):
    def test_exactly_eight_static_variants_have_expected_identity_and_price(self):
        actual = {
            (item.rest_path, item.mcp_resource): item.price
            for item in V111_VARIANTS
        }
        self.assertEqual(actual, {
            ("/search-and-extract/results", "mcp://tool/search_and_extract/results"): "$0.05",
            ("/search-and-extract/answer", "mcp://tool/search_and_extract/answer"): "$0.10",
            ("/search-and-extract/structured", "mcp://tool/search_and_extract/structured"): "$0.15",
            ("/extract-structured-data/webpage", "mcp://tool/extract_structured_data/webpage"): "$0.05",
            ("/extract-structured-data/image", "mcp://tool/extract_structured_data/image"): "$0.05",
            ("/extract-structured-data/pdf", "mcp://tool/extract_structured_data/pdf"): "$0.05",
            ("/extract-structured-data/audio", "mcp://tool/extract_structured_data/audio"): "$0.10",
            ("/extract-structured-data/video", "mcp://tool/extract_structured_data/video"): "$0.15",
        })

    def test_variants_are_closed_and_do_not_embed_payment_configuration(self):
        for item in V111_VARIANTS:
            self.assertIn(item.capability, {"search_and_extract", "extract_structured_data"})
            self.assertNotIn("network", item.__dataclass_fields__)
            self.assertNotIn("asset", item.__dataclass_fields__)
            self.assertNotIn("payee", item.__dataclass_fields__)


class SearchRequestTests(unittest.TestCase):
    def test_request_schemas_take_ownership_of_nested_caller_data(self):
        schema = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["stable"]},
            },
            "required": ["status"],
            "additionalProperties": False,
        }
        search = SearchAndExtractRequest.model_validate({
            "query": "q",
            "mode": "structured",
            "json_schema": schema,
        })
        direct = DirectExtractionRequest.model_validate({
            "source_type": "webpage",
            "source_url": "https://example.com/",
            "json_schema": schema,
        })

        schema["properties"]["status"]["enum"].append("mutated")
        schema["properties"]["extra"] = {"type": "string"}
        schema["outside"] = True

        for contract in (search, direct):
            owned = contract.json_schema
            self.assertEqual(owned["properties"]["status"]["enum"], ["stable"])
            self.assertNotIn("extra", owned["properties"])
            self.assertNotIn("outside", owned)
        self.assertIsNot(search.json_schema, direct.json_schema)
        self.assertIsNot(
            search.json_schema["properties"]["status"],
            direct.json_schema["properties"]["status"],
        )
        self.assertEqual(
            SearchAndExtractRequest.model_validate(search.model_dump()).json_schema,
            search.json_schema,
        )

    def test_request_schemas_reject_cyclic_and_non_json_values_safely(self):
        cyclic = {"type": "object", "properties": {}, "additionalProperties": False}
        cyclic["properties"]["cycle"] = cyclic
        non_json = {
            "type": "object",
            "properties": {"value": {"type": "string", "const": object()}},
            "additionalProperties": False,
        }
        for schema in (cyclic, non_json):
            with self.subTest(schema_type=type(schema).__name__), self.assertRaises(ValidationError):
                SearchAndExtractRequest.model_validate({
                    "query": "q",
                    "mode": "structured",
                    "json_schema": schema,
                })

    def test_defaults_and_normalization_match_design(self):
        request = SearchAndExtractRequest.model_validate({
            "query": "  official x402 docs  ",
            "mode": "results",
            "domains": ["Docs.X402.Org."],
        })
        self.assertEqual(request.query, "official x402 docs")
        self.assertEqual(request.max_results, 5)
        self.assertEqual(request.domains, ("docs.x402.org",))

    def test_whitespace_is_trimmed_before_string_length_limits(self):
        query = "q" * 500
        instructions = "i" * 2000
        request = SearchAndExtractRequest.model_validate({
            "query": f"  {query}  ",
            "mode": "structured",
            "json_schema": MINIMAL_SCHEMA,
            "instructions": f"\n{instructions}\t",
        })
        self.assertEqual(request.query, query)
        self.assertEqual(request.instructions, instructions)

        for field, value in (("query", "q" * 501), ("instructions", "i" * 2001)):
            payload = {
                "query": "q",
                "mode": "structured",
                "json_schema": MINIMAL_SCHEMA,
                "instructions": "instructions",
                field: f" {value} ",
            }
            with self.subTest(field=field), self.assertRaises(ValidationError):
                SearchAndExtractRequest.model_validate(payload)

    def test_structured_mode_defaults_sources_and_requires_schema(self):
        request = SearchAndExtractRequest.model_validate({
            "query": "release details",
            "mode": "structured",
            "json_schema": MINIMAL_SCHEMA,
        })
        self.assertEqual(request.max_sources, 3)
        with self.assertRaises(ValidationError):
            SearchAndExtractRequest.model_validate({
                "query": "release details",
                "mode": "structured",
            })

    def test_cross_field_rules_fail_closed(self):
        invalid = [
            {"query": "q", "mode": "results", "json_schema": MINIMAL_SCHEMA},
            {"query": "q", "mode": "answer", "instructions": "do it"},
            {"query": "q", "mode": "results", "max_sources": 1},
            {
                "query": "q",
                "mode": "structured",
                "json_schema": MINIMAL_SCHEMA,
                "max_results": 2,
                "max_sources": 3,
            },
            {"query": "q", "mode": "unknown"},
            {"query": "q", "mode": "results", "unexpected": True},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SearchAndExtractRequest.model_validate(value)

    def test_integer_limits_do_not_coerce_strings_or_booleans(self):
        for field, value in [
            ("max_results", "5"),
            ("max_results", True),
            ("max_sources", "2"),
            ("max_sources", False),
        ]:
            request = {
                "query": "q",
                "mode": "structured",
                "json_schema": MINIMAL_SCHEMA,
                field: value,
            }
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                SearchAndExtractRequest.model_validate(request)

    def test_request_contracts_apply_the_bounded_schema_guard(self):
        unsafe_schema = {
            "type": "object",
            "properties": {
                "value": {"type": "string", "$ref": "https://invalid.example/schema"},
            },
            "required": ["value"],
            "additionalProperties": False,
        }
        requests = [
            {
                "query": "q",
                "mode": "structured",
                "json_schema": unsafe_schema,
            },
            {
                "source_type": "webpage",
                "source_url": "https://example.com/",
                "json_schema": unsafe_schema,
            },
        ]
        for model, request in zip(
            (SearchAndExtractRequest, DirectExtractionRequest),
            requests,
            strict=True,
        ):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(request)

    def test_domains_are_unique_public_dns_names_only(self):
        invalid_domains = [
            ["example.com", "EXAMPLE.COM"],
            ["https://example.com"],
            ["user:pass@example.com"],
            ["example.com:443"],
            ["example.com/path"],
            ["*.example.com"],
            ["127.0.0.1"],
            ["0x7f000001"],
            ["localhost"],
            [("é." * 40) + "com"],
        ]
        for domains in invalid_domains:
            with self.subTest(domains=domains), self.assertRaises(ValidationError):
                SearchAndExtractRequest.model_validate({
                    "query": "q",
                    "mode": "results",
                    "domains": domains,
                })


class DirectExtractionRequestTests(unittest.TestCase):
    def test_webpage_defaults_to_auto_rendering(self):
        request = DirectExtractionRequest.model_validate({
            "source_type": "webpage",
            "source_url": "https://example.com/report",
            "json_schema": MINIMAL_SCHEMA,
        })
        self.assertEqual(request.render_mode, "auto")

    def test_media_forbids_render_mode_and_unknown_fields(self):
        for value in [
            {
                "source_type": "pdf",
                "source_url": "https://cdn.example.com/report.pdf",
                "render_mode": "auto",
                "json_schema": MINIMAL_SCHEMA,
            },
            {
                "source_type": "image",
                "source_url": "https://cdn.example.com/image.png",
                "json_schema": MINIMAL_SCHEMA,
                "mode": "structured",
            },
        ]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                DirectExtractionRequest.model_validate(value)

    def test_source_url_is_bounded_public_https_without_credentials_or_port(self):
        invalid_urls = [
            "http://example.com/file.pdf",
            "https://user:pass@example.com/file.pdf",
            "https://example.com:444/file.pdf",
            "https://127.0.0.1/file.pdf",
            "https://localhost/file.pdf",
            "https://example.com/" + ("x" * 4090),
        ]
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                DirectExtractionRequest.model_validate({
                    "source_type": "pdf",
                    "source_url": url,
                    "json_schema": MINIMAL_SCHEMA,
                })

    def test_source_url_and_instructions_trim_before_length_limits(self):
        prefix = "https://example.com/"
        source_url = prefix + ("a" * (4096 - len(prefix)))
        instructions = "i" * 2000
        request = DirectExtractionRequest.model_validate({
            "source_type": "webpage",
            "source_url": f"  {source_url}\n",
            "json_schema": MINIMAL_SCHEMA,
            "instructions": f"\t{instructions} ",
        })
        self.assertEqual(request.source_url, source_url)
        self.assertEqual(request.instructions, instructions)

        with self.assertRaises(ValidationError):
            DirectExtractionRequest.model_validate({
                "source_type": "webpage",
                "source_url": source_url + "a",
                "json_schema": MINIMAL_SCHEMA,
            })


class ResponseContractTests(unittest.TestCase):
    def setUp(self):
        self.common = {
            "request_id": "a1b2c3d4e5f60708",
            "service_version": "1.11.0",
            "retrieved_at": "2026-09-02T15:04:05Z",
        }

    def test_results_caps_and_unknown_properties_are_enforced(self):
        result = SearchResultsResponse.model_validate({
            **self.common,
            "query": "query",
            "results": [{
                "source_id": "s1",
                "rank": 1,
                "title": "title",
                "url": "https://example.com",
                "snippet": "snippet",
            }],
        })
        self.assertEqual(result.mode, "results")
        with self.assertRaises(ValidationError):
            SearchResultsResponse.model_validate({
                **self.common,
                "query": "query",
                "results": [{
                    "source_id": "s1",
                    "rank": 1,
                    "title": "title",
                    "url": "https://example.com",
                    "snippet": "snippet",
                }],
                "provider_response": {"secret": "canary"},
            })

    def test_evidence_contract_accepts_the_rfc_6901_root_pointer(self):
        evidence = EvidenceEntry.model_validate({
            "field": "",
            "source_id": "s1",
            "quote": "whole value",
        })
        self.assertEqual(evidence.field, "")

    def test_all_response_timestamps_require_timezone_aware_utc(self):
        result_payload = {
            **self.common,
            "query": "query",
            "freshness_after": "2026-09-01T00:00:00Z",
            "results": [{
                "source_id": "s1",
                "rank": 1,
                "title": "title",
                "url": "https://example.com",
                "snippet": "snippet",
                "published_at": "2026-09-01T12:00:00Z",
            }],
        }
        parsed = SearchResultsResponse.model_validate(result_payload)
        self.assertEqual(parsed.retrieved_at.utcoffset().total_seconds(), 0)
        self.assertIn('"retrieved_at":"2026-09-02T15:04:05Z"', parsed.model_dump_json())

        invalid_paths = (
            ("retrieved_at", None),
            ("freshness_after", None),
            ("published_at", "results"),
        )
        for field, container in invalid_paths:
            for invalid in ("2026-09-02T15:04:05", "2026-09-02T16:04:05+01:00"):
                payload = {
                    **result_payload,
                    "results": [dict(result_payload["results"][0])],
                }
                if container is None:
                    payload[field] = invalid
                else:
                    payload[container][0][field] = invalid
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValidationError):
                    SearchResultsResponse.model_validate(payload)

        answer_payload = {
            **self.common,
            "query": "query",
            "answer": "answer",
            "claims": [{"text": "claim", "citation_ids": ["c1"]}],
            "citations": [{
                "citation_id": "c1",
                "title": "title",
                "url": "https://example.com",
                "published_at": "2026-09-02T16:04:05+01:00",
            }],
        }
        with self.assertRaises(ValidationError):
            SearchAnswerResponse.model_validate(answer_payload)

        structured_payload = {
            **self.common,
            "mode": "structured",
            "data": {"title": "Example"},
            "sources": [{
                "source_id": "s1",
                "title": "title",
                "url": "https://example.com",
                "retrieval_method": "http",
                "retrieved_at": "2026-09-02T15:04:05",
            }],
            "evidence": [],
            "missing_fields": [],
            "uncertainties": [],
        }
        with self.assertRaises(ValidationError):
            StructuredResponse.model_validate(structured_payload)

    def test_empty_search_deliveries_are_not_valid_successes(self):
        with self.assertRaises(ValidationError):
            SearchResultsResponse.model_validate({
                **self.common,
                "query": "query",
                "results": [],
            })
        with self.assertRaises(ValidationError):
            SearchAnswerResponse.model_validate({
                **self.common,
                "query": "query",
                "answer": "unsupported answer",
                "claims": [],
                "citations": [],
            })

    def test_failure_codes_are_limited_to_the_documented_contract(self):
        value = {
            "request_id": "a1b2c3d4e5f60708",
            "service_version": "1.11.0",
            "error_code": "schema_validation_failed",
            "error": "The extracted result did not satisfy the requested schema.",
        }
        self.assertEqual(
            FailureResponse.model_validate(value).error_code,
            "schema_validation_failed",
        )
        value["error_code"] = "provider_secret_canary"
        with self.assertRaises(ValidationError):
            FailureResponse.model_validate(value)

    def test_answer_claims_must_reference_unique_returned_citations(self):
        value = {
            **self.common,
            "query": "query",
            "answer": "answer",
            "claims": [{"text": "claim", "citation_ids": ["c1"]}],
            "citations": [{
                "citation_id": "c1",
                "title": "title",
                "url": "https://example.com",
            }],
        }
        self.assertEqual(SearchAnswerResponse.model_validate(value).mode, "answer")
        value["claims"][0]["citation_ids"] = ["unknown"]
        with self.assertRaises(ValidationError):
            SearchAnswerResponse.model_validate(value)

        value["claims"][0]["citation_ids"] = ["c1"]
        value["citations"][0]["source_id"] = "internal-source-id"
        with self.assertRaises(ValidationError):
            SearchAnswerResponse.model_validate(value)

    def test_structured_shape_distinguishes_search_and_direct_results(self):
        source = {
            "source_id": "s1",
            "title": "title",
            "url": "https://example.com",
            "retrieval_method": "http",
            "retrieved_at": "2026-09-02T15:04:04Z",
        }
        shared = {
            **self.common,
            "data": {"title": "Example"},
            "sources": [source],
            "evidence": [{"field": "/title", "source_id": "s1", "quote": "Example"}],
            "missing_fields": [],
            "uncertainties": [],
        }
        self.assertEqual(
            StructuredResponse.model_validate({**shared, "mode": "structured"}).mode,
            "structured",
        )
        direct = StructuredResponse.model_validate({
            **shared,
            "source_type": "webpage",
            "retrieval_method": "http",
        })
        self.assertEqual(direct.source_type, "webpage")
        with self.assertRaises(ValidationError):
            StructuredResponse.model_validate({
                **shared,
                "mode": "structured",
                "source_type": "webpage",
                "retrieval_method": "http",
            })

    def test_structured_retrieval_methods_match_the_capability(self):
        source = {
            "source_id": "s1",
            "title": "title",
            "url": "https://example.com",
            "retrieval_method": "image",
            "retrieved_at": "2026-09-02T15:04:04Z",
        }
        shared = {
            **self.common,
            "data": {"title": "Example"},
            "sources": [source],
            "evidence": [{"field": "/title", "source_id": "s1", "description": "Example"}],
            "missing_fields": [],
            "uncertainties": [],
            "source_type": "image",
        }
        valid = StructuredResponse.model_validate({
            **shared,
            "retrieval_method": "image",
        })
        self.assertEqual(valid.retrieval_method, "image")

        for update in [
            {"retrieval_method": "browser"},
            {
                "retrieval_method": "image",
                "sources": [{**source, "retrieval_method": "browser"}],
            },
        ]:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                StructuredResponse.model_validate({**shared, **update})

        search = {key: value for key, value in shared.items() if key != "source_type"}
        with self.assertRaises(ValidationError):
            StructuredResponse.model_validate({**search, "mode": "structured"})


if __name__ == "__main__":
    unittest.main()
