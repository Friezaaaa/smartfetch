import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "v111"
MODEL_IDS = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
PREFIX_VARIANTS = {
    "RES": "results",
    "ANS": "answer",
    "SRS": "structured",
    "WEB": "webpage",
    "IMG": "image",
    "PDF": "pdf",
    "AUD": "audio",
    "VID": "video",
}


class Stage5ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((BENCHMARK / "cases.json").read_text("utf-8"))
        cls.fixtures = json.loads((BENCHMARK / "fixtures.json").read_text("utf-8"))

    def test_exact_case_and_variant_inventory(self):
        cases = self.manifest["cases"]
        self.assertEqual(self.manifest["manifest_version"], 1)
        self.assertEqual(len(cases), 32)
        self.assertEqual(
            [case["case_id"] for case in cases],
            [f"{prefix}-{index:02d}" for prefix in PREFIX_VARIANTS for index in range(1, 5)],
        )
        for prefix, variant in PREFIX_VARIANTS.items():
            selected = [case for case in cases if case["case_id"].startswith(prefix)]
            self.assertEqual(len(selected), 4)
            self.assertTrue(all(case["variant"] == variant for case in selected))
            self.assertTrue(all(case["expected_failure_code"] is None for case in selected))

    def test_search_requests_preserve_the_approved_contract(self):
        cases = self.manifest["cases"][:12]
        self.assertTrue(all(case["request"]["max_results"] == 5 for case in cases))
        for case in cases[:8]:
            self.assertNotIn("max_sources", case["request"])
        for case in cases[8:]:
            self.assertEqual(case["request"]["max_sources"], 3)

    def test_exact_model_and_provider_trial_inventory(self):
        cases = self.manifest["cases"]
        for case in cases[:4]:
            self.assertEqual(case["providers"], ["exa"])
            self.assertEqual(case["models"], [])
        for case in cases[4:]:
            self.assertEqual(case["models"], MODEL_IDS)
        self.assertEqual(sum("exa" in case["providers"] for case in cases), 12)
        self.assertEqual(sum(len(case["models"]) for case in cases), 56)

    def test_structured_schemas_are_strict_and_expected_values_stay_in_oracle(self):
        for case in self.manifest["cases"][8:]:
            schema = case["request"]["json_schema"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            encoded_schema = json.dumps(schema, sort_keys=True)
            encoded_oracle = json.dumps(case["oracle"], sort_keys=True)
            for expected in case["oracle"].get("expected", {}).values():
                if expected is not None and not isinstance(expected, (dict, list)):
                    self.assertNotIn(json.dumps(expected), encoded_schema)
            self.assertTrue(encoded_oracle)

    def test_fixture_manifest_hashes_sizes_and_corpus_bound(self):
        entries = self.fixtures["fixtures"]
        self.assertEqual(len(entries), 16)
        self.assertEqual({entry["fixture_id"] for entry in entries}, {
            f"{prefix}-{index:02d}"
            for prefix in ("IMG", "PDF", "AUD", "VID")
            for index in range(1, 5)
        })
        total = 0
        for entry in entries:
            self.assertEqual(
                set(entry),
                {
                    "fixture_id", "path", "byte_size", "sha256", "mime_type",
                    "expected_semantic_content",
                },
            )
            self.assertIn(entry["mime_type"], {
                "image/png", "application/pdf", "audio/wav", "video/webm",
            })
            self.assertIsInstance(entry["expected_semantic_content"], dict)
            path = BENCHMARK / entry["path"]
            data = path.read_bytes()
            self.assertEqual(len(data), entry["byte_size"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
            self.assertFalse(path.is_symlink())
            total += len(data)
        self.assertLess(total, 8 * 1024 * 1024)

    def test_fixture_cases_reference_only_hashed_local_assets(self):
        fixture_by_case = {entry["fixture_id"]: entry for entry in self.fixtures["fixtures"]}
        for case in self.manifest["cases"][16:]:
            self.assertEqual(case["fixture"], fixture_by_case[case["case_id"]]["path"])
            self.assertEqual(
                case["oracle"]["expected"],
                fixture_by_case[case["case_id"]]["expected_semantic_content"],
            )
            self.assertNotIn("source_url", case["request"])


if __name__ == "__main__":
    unittest.main()
