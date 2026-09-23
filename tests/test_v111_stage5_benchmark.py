"""No-network Stage 5 benchmark contract tests."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
import tempfile
import shutil
import unittest
from unittest.mock import patch

from smartfetch.benchmark_v111 import (
    BenchmarkFailure,
    BenchmarkLedger,
    TrialOutcome,
    TrialRecord,
    RealAuthorization,
    authorize_real_run,
    build_trial_inventory,
    load_benchmark_manifest,
    render_reports,
    run_injected_trials,
)
from smartfetch.costs import ProviderUsage


ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "v111"
GOOD_ENV = {
    "SMARTFETCH_BENCHMARK_APPROVED_BUDGET_MICROUSD": "1000000",
    "SMARTFETCH_BENCHMARK_APPROVAL": "V111_32_CASE_REAL",
    "EXA_API_KEY": "hidden-exa-canary",
    "GEMINI_API_KEY": "hidden-gemini-canary",
}
CAPS = {"exa": 1000, "gemini-3.8-flash": 2000, "gemini-3.5-flash-lite": 2000}


class Stage5BenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_benchmark_manifest(ROOT)

    def test_exact_inventory_and_shared_candidate_input(self) -> None:
        inventory = build_trial_inventory(self.manifest)
        self.assertEqual(len(self.manifest.cases), 32)
        self.assertEqual(len(inventory), 68)
        self.assertEqual(sum(trial.provider == "exa" for trial in inventory), 12)
        self.assertEqual(sum(trial.provider == "gemini" for trial in inventory), 56)
        for case in self.manifest.cases[4:]:
            candidates = [trial for trial in inventory if trial.case_id == case.case_id and trial.provider == "gemini"]
            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0].input_package, candidates[1].input_package)
            self.assertIs(candidates[0].input_package, candidates[1].input_package)
        orders = [
            next(trial.model_id for trial in inventory if trial.case_id == case.case_id and trial.provider == "gemini")
            for case in self.manifest.cases[4:]
        ]
        self.assertEqual(orders[:4], ["gemini-3.8-flash", "gemini-3.5-flash-lite"] * 2)

    def test_manifest_tampering_rejected_before_executor(self) -> None:
        raw = json.loads((ROOT / "cases.json").read_text("utf-8"))
        raw["cases"][0]["request"]["max_results"] = 6
        with self.assertRaisesRegex(BenchmarkFailure, "invalid_benchmark_manifest"):
            load_benchmark_manifest(ROOT, raw_manifest=raw)

    def test_fixture_hash_tampering_fails_before_work(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name)
            shutil.copytree(ROOT, destination / "v111")
            (destination / "v111" / "fixtures" / "AUD-01.wav").write_bytes(b"canary fixture mutation")
            with self.assertRaisesRegex(BenchmarkFailure, "invalid_benchmark_manifest"):
                load_benchmark_manifest(destination / "v111")
        raw = json.loads((ROOT / "cases.json").read_text("utf-8"))
        raw["cases"][4]["request"]["max_sources"] = 3
        with self.assertRaisesRegex(BenchmarkFailure, "invalid_benchmark_manifest"):
            load_benchmark_manifest(ROOT, raw_manifest=raw)

    def test_offline_execution_never_reads_environment_or_executor(self) -> None:
        class ForbiddenEnvironment(dict):
            def __getitem__(self, key):
                raise AssertionError("environment accessed")

            def get(self, key, default=None):
                raise AssertionError("environment accessed")

        calls = []
        records = run_injected_trials(
            self.manifest,
            execute_real_providers=False,
            environment=ForbiddenEnvironment(),
            executor=lambda _: calls.append("called"),
        )
        self.assertEqual(records, ())
        self.assertEqual(calls, [])

    def test_cli_defaults_to_offline_manifest_validation(self) -> None:
        from scripts.benchmark_v111 import main

        class ForbiddenEnvironment(dict):
            def get(self, key, default=None):
                if key in {"EXA_API_KEY", "GEMINI_API_KEY", "SMARTFETCH_BENCHMARK_APPROVAL"}:
                    raise AssertionError("credential environment accessed")
                return default

        with patch("scripts.benchmark_v111.os.environ", ForbiddenEnvironment()):
            self.assertEqual(main([]), 0)
        with patch("scripts.benchmark_v111.os.environ", ForbiddenEnvironment()):
            self.assertEqual(main(["--execute-real-providers"]), 2)

    def test_real_authorization_is_exact_and_pre_reserves_run_maximum(self) -> None:
        authorization = authorize_real_run(
            self.manifest,
            execute_real_providers=True,
            max_total_cost_microusd="1000000",
            environment=GOOD_ENV,
            operation_caps=CAPS,
        )
        self.assertEqual(authorization.maximum_reserved_micro_usd, 124000)
        self.assertEqual(authorization.budget_micro_usd, 1000000)
        for bad in ("0", "10000001", "+5000", "５０００", "01", "5.0", " 5", "-1"):
            env = {**GOOD_ENV, "SMARTFETCH_BENCHMARK_APPROVED_BUDGET_MICROUSD": bad}
            with self.subTest(bad=bad), self.assertRaisesRegex(BenchmarkFailure, "benchmark_not_authorized"):
                authorize_real_run(self.manifest, execute_real_providers=True, max_total_cost_microusd=bad,
                                   environment=env, operation_caps=CAPS)
        for key in GOOD_ENV:
            env = {k: value for k, value in GOOD_ENV.items() if k != key}
            with self.subTest(missing=key), self.assertRaisesRegex(BenchmarkFailure, "benchmark_not_authorized"):
                authorize_real_run(self.manifest, execute_real_providers=True, max_total_cost_microusd="1000000",
                                   environment=env, operation_caps=CAPS)
        with self.assertRaisesRegex(BenchmarkFailure, "benchmark_budget_exceeded"):
            authorize_real_run(self.manifest, execute_real_providers=True, max_total_cost_microusd="1000000",
                               environment=GOOD_ENV, operation_caps={**CAPS, "gemini-3.8-flash": 100000})

    def test_execution_boundary_rederives_authorization_before_executor(self) -> None:
        authorization = authorize_real_run(
            self.manifest,
            execute_real_providers=True,
            max_total_cost_microusd="1000000",
            environment=GOOD_ENV,
            operation_caps=CAPS,
        )
        calls = []

        def executor(trial):
            calls.append(trial.case_id)
            return TrialOutcome(
                success=True,
                failure_code=None,
                usage=ProviderUsage(provider=trial.provider, cost_micro_usd=1),
                latency_ms=1,
                contract_valid=True,
                evidence_valid=True,
                citation_valid=True,
                source_count=1,
            )

        invalid_attempts = (
            (authorization, {}, "1000000", CAPS, "empty environment"),
            (authorization, {key: value for key, value in GOOD_ENV.items()
                             if key != "SMARTFETCH_BENCHMARK_APPROVAL"}, "1000000", CAPS, "missing approval"),
            (authorization, {key: value for key, value in GOOD_ENV.items()
                             if key != "EXA_API_KEY"}, "1000000", CAPS, "missing Exa credential"),
            (authorization, {key: value for key, value in GOOD_ENV.items()
                             if key != "GEMINI_API_KEY"}, "1000000", CAPS, "missing Gemini credential"),
            (replace(authorization, manifest_hash="0" * 64), GOOD_ENV, "1000000", CAPS, "manifest hash"),
            (replace(
                authorization,
                operation_caps=tuple(sorted({**CAPS, "exa": 999}.items())),
                maximum_reserved_micro_usd=123988,
            ), GOOD_ENV, "1000000", CAPS, "operation caps"),
            (replace(authorization, budget_micro_usd=999999), GOOD_ENV, "1000000", CAPS, "authorization budget"),
            (authorization, GOOD_ENV, "999999", CAPS, "CLI/environment budget"),
        )
        for supplied, environment, budget, caps, label in invalid_attempts:
            with self.subTest(label=label), self.assertRaisesRegex(
                BenchmarkFailure, "benchmark_not_authorized"
            ):
                run_injected_trials(
                    self.manifest,
                    execute_real_providers=True,
                    authorization=supplied,
                    environment=environment,
                    max_total_cost_microusd=budget,
                    operation_caps=caps,
                    executor=executor,
                )
            self.assertEqual(calls, [])

        for budget in ("0", "-1", "bad", "123999"):
            environment = {
                **GOOD_ENV,
                "SMARTFETCH_BENCHMARK_APPROVED_BUDGET_MICROUSD": budget,
            }
            expected = (
                "benchmark_budget_exceeded" if budget == "123999"
                else "benchmark_not_authorized"
            )
            with self.subTest(budget=budget), self.assertRaisesRegex(BenchmarkFailure, expected):
                run_injected_trials(
                    self.manifest,
                    execute_real_providers=True,
                    authorization=authorization,
                    environment=environment,
                    max_total_cost_microusd=budget,
                    operation_caps=CAPS,
                    executor=executor,
                )
            self.assertEqual(calls, [])

    def test_authorized_execution_and_end_to_end_candidate_costs(self) -> None:
        authorization = authorize_real_run(
            self.manifest,
            execute_real_providers=True,
            max_total_cost_microusd="1000000",
            environment=GOOD_ENV,
            operation_caps=CAPS,
        )

        def executor(trial):
            return TrialOutcome(
                success=True,
                failure_code=None,
                usage=ProviderUsage(
                    provider=trial.provider,
                    cost_micro_usd=100 if trial.provider == "exa" else 200,
                ),
                latency_ms=1,
                contract_valid=True,
                evidence_valid=True,
                citation_valid=True,
                source_count=1,
            )

        records = run_injected_trials(
            self.manifest,
            execute_real_providers=True,
            authorization=authorization,
            environment=GOOD_ENV,
            max_total_cost_microusd="1000000",
            operation_caps=CAPS,
            executor=executor,
        )
        answer = [record for record in records if record.case_id == "ANS-01"]
        structured = [record for record in records if record.case_id == "SRS-01"]
        for candidates in (answer, structured):
            self.assertEqual(
                [(record.provider, record.provider_cost_micro_usd, record.total_cost_micro_usd)
                 for record in candidates],
                [("exa", 100, 100), ("gemini", 200, 300), ("gemini", 200, 300)],
            )
        results = [record for record in records if record.case_id == "RES-01"]
        image = [record for record in records if record.case_id == "IMG-01"]
        self.assertEqual([(record.provider_cost_micro_usd, record.total_cost_micro_usd)
                          for record in results], [(100, 100)])
        self.assertEqual([(record.provider_cost_micro_usd, record.total_cost_micro_usd)
                          for record in image], [(200, 200), (200, 200)])

        report = json.loads(render_reports(self.manifest, records)[0])
        self.assertEqual(report["summary"]["total_cost_micro_usd"], 12 * 100 + 56 * 200)
        for variant in ("answer", "structured"):
            summary = report["summary"]["variants"][variant]
            self.assertEqual(summary["trial_count"], 8)
            self.assertEqual(summary["provider_cost_micro_usd"], 4 * 100 + 8 * 200)
            self.assertEqual(summary["candidate_total_cost_micro_usd"], 8 * 300)
            self.assertEqual(summary["median_cost_micro_usd"], 300)
            self.assertEqual(summary["maximum_cost_micro_usd"], 300)
        self.assertEqual(report["summary"]["variants"]["results"]["median_cost_micro_usd"], 100)
        self.assertEqual(report["summary"]["variants"]["image"]["median_cost_micro_usd"], 200)

    def test_budget_reservations_are_exact_and_single_use(self) -> None:
        ledger = BenchmarkLedger(3000)
        token = ledger.reserve(2000)
        with self.assertRaisesRegex(BenchmarkFailure, "benchmark_budget_exceeded"):
            ledger.reserve(1001)
        ledger.commit(token, 1500)
        self.assertEqual(ledger.spent_micro_usd, 1500)
        with self.assertRaisesRegex(BenchmarkFailure, "invalid_benchmark_usage"):
            ledger.commit(token, 100)
        token = ledger.reserve(1000)
        ledger.release(token)
        self.assertEqual(ledger.spent_micro_usd, 1500)
        with self.assertRaisesRegex(BenchmarkFailure, "invalid_benchmark_usage"):
            ledger.reserve(True)

    def test_no_retry_mandatory_usage_and_failure_preservation(self) -> None:
        seen = []

        def fake(trial):
            seen.append((trial.case_id, trial.model_id))
            if trial.case_id == "RES-01":
                return TrialOutcome(success=False, failure_code="invalid_provider_output", usage=ProviderUsage(
                    provider="exa", search_queries=1, cost_micro_usd=120), latency_ms=12,
                    contract_valid=False, evidence_valid=False, citation_valid=False, source_count=0)
            return TrialOutcome(success=True, failure_code=None, usage=ProviderUsage(
                provider=trial.provider, search_queries=1 if trial.provider == "exa" else 0,
                cost_micro_usd=100), latency_ms=4,
                contract_valid=True, evidence_valid=True, citation_valid=True, source_count=1)

        authorization = authorize_real_run(self.manifest, execute_real_providers=True,
                                           max_total_cost_microusd="1000000", environment=GOOD_ENV,
                                           operation_caps=CAPS)
        records = run_injected_trials(self.manifest, execute_real_providers=True,
                                      authorization=authorization, environment=GOOD_ENV,
                                      max_total_cost_microusd="1000000", operation_caps=CAPS,
                                      executor=fake)
        self.assertEqual(len(records), 68)
        self.assertEqual(len(seen), 68)
        self.assertEqual(records[0].failure_code, "invalid_provider_output")
        self.assertFalse(records[0].success)
        with self.assertRaisesRegex(BenchmarkFailure, "invalid_benchmark_usage"):
            run_injected_trials(self.manifest, execute_real_providers=True,
                                authorization=authorization, environment=GOOD_ENV,
                                max_total_cost_microusd="1000000", operation_caps=CAPS,
                                executor=lambda trial: TrialOutcome(
                                    success=True, failure_code=None, usage=None, latency_ms=1,
                                    contract_valid=True, evidence_valid=True, citation_valid=True, source_count=1))

    def test_forged_authorization_and_executor_exception_fail_finitely(self) -> None:
        forged = RealAuthorization(self.manifest.sha256, 1000000, 0, (("exa", 1),))
        with self.assertRaisesRegex(BenchmarkFailure, "benchmark_not_authorized"):
            run_injected_trials(self.manifest, execute_real_providers=True,
                                authorization=forged, executor=lambda trial: None)
        authorization = authorize_real_run(self.manifest, execute_real_providers=True,
                                           max_total_cost_microusd="1000000", environment=GOOD_ENV,
                                           operation_caps=CAPS)
        calls = []

        def hostile(trial):
            calls.append(trial.case_id)
            raise RuntimeError("CANARY_RAW_PROVIDER_RESPONSE")

        with self.assertRaisesRegex(BenchmarkFailure, "invalid_benchmark_result") as failure:
            run_injected_trials(self.manifest, execute_real_providers=True,
                                authorization=authorization, environment=GOOD_ENV,
                                max_total_cost_microusd="1000000", operation_caps=CAPS,
                                executor=hostile)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("CANARY", str(failure.exception))

    def test_report_rejects_injected_failure_text(self) -> None:
        authorization = authorize_real_run(self.manifest, execute_real_providers=True,
                                           max_total_cost_microusd="1000000", environment=GOOD_ENV,
                                           operation_caps=CAPS)
        records = run_injected_trials(self.manifest, execute_real_providers=True,
                                      authorization=authorization, environment=GOOD_ENV,
                                      max_total_cost_microusd="1000000", operation_caps=CAPS,
                                      executor=lambda trial: TrialOutcome(
                                          success=False, failure_code="unknown", usage=ProviderUsage(
                                              provider=trial.provider, cost_micro_usd=1), latency_ms=1,
                                          contract_valid=False, evidence_valid=False, citation_valid=False,
                                          source_count=0))
        first = records[0]
        forged = TrialRecord(*[getattr(first, field) if field != "failure_code" else "CANARY_PRIVATE_URL"
                               for field in first.__dataclass_fields__])
        with self.assertRaisesRegex(BenchmarkFailure, "invalid_benchmark_result"):
            render_reports(self.manifest, (forged, *records[1:]))

    def test_reports_allowlist_only_bounded_measurements(self) -> None:
        secret = "CANARY_PROVIDER_PAYLOAD_private-url_or_key"
        outcome = TrialOutcome(success=True, failure_code=None,
                               usage=ProviderUsage(provider="gemini", input_tokens=4, output_tokens=2,
                                                   cost_micro_usd=100), latency_ms=7,
                               contract_valid=True, evidence_valid=True, citation_valid=True, source_count=1)
        authorization = authorize_real_run(self.manifest, execute_real_providers=True,
                                           max_total_cost_microusd="1000000", environment=GOOD_ENV,
                                           operation_caps=CAPS)
        records = run_injected_trials(self.manifest, execute_real_providers=True,
                                      authorization=authorization, environment=GOOD_ENV,
                                      max_total_cost_microusd="1000000", operation_caps=CAPS,
                                      executor=lambda trial: outcome if trial.provider == "gemini" else TrialOutcome(
                                          success=False, failure_code="provider_unavailable", usage=ProviderUsage(
                                              provider="exa", cost_micro_usd=100), latency_ms=1,
                                          contract_valid=False, evidence_valid=False, citation_valid=False,
                                          source_count=0))
        json_text, markdown = render_reports(self.manifest, records)
        for output in (json_text, markdown):
            self.assertNotIn(secret, output)
            self.assertNotIn(GOOD_ENV["EXA_API_KEY"], output)
            self.assertNotIn("P95", output)
            self.assertLess(len(output.encode("utf-8")), 128_000)
        report = json.loads(json_text)
        self.assertEqual(report["manifest_hash"], self.manifest.sha256)
        self.assertEqual(report["summary"]["trial_count"], 68)


if __name__ == "__main__":
    unittest.main()
