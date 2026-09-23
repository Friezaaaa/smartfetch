"""Offline-default, injected V1.11 benchmark preparation and accounting.

This module owns no provider client or credential source. The only executable
path receives a separately authorized, injected operation callable.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from statistics import median_low
import threading
from typing import Callable, Mapping

from .costs import ProviderUsage


APPROVED_CASES_SHA256 = "9d633612571f7d01ffd637b8de8ee1cd877f031f23775c54a4aa395735584b16"
APPROVED_FIXTURES_SHA256 = "39957ac308528293d37320366a6a4017c9eeafe5466f73128f161ac92d775037"
MODEL_IDS = ("gemini-3.8-flash", "gemini-3.5-flash-lite")
PREFIX_VARIANTS = (
    ("RES", "results"), ("ANS", "answer"), ("SRS", "structured"),
    ("WEB", "webpage"), ("IMG", "image"), ("PDF", "pdf"),
    ("AUD", "audio"), ("VID", "video"),
)
FAILURE_CODES = frozenset({
    "provider_unavailable", "provider_timeout", "invalid_provider_output",
    "invalid_schema", "insufficient_evidence", "invalid_citation",
    "retrieval_failed", "unsupported_media", "capacity_unavailable",
    "benchmark_budget_exceeded", "unknown",
})
MAX_BUDGET = 10_000_000
MAX_REPORT_BYTES = 128_000


class BenchmarkFailure(ValueError):
    def __init__(self, code: str) -> None:
        if code not in {
            "invalid_benchmark_manifest", "benchmark_not_authorized",
            "benchmark_budget_exceeded", "invalid_benchmark_usage",
            "invalid_benchmark_result", "benchmark_report_too_large",
        }:
            code = "invalid_benchmark_result"
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    variant: str
    providers: tuple[str, ...]
    models: tuple[str, ...]
    input_package: bytes


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    root: Path
    sha256: str
    cases: tuple[BenchmarkCase, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkTrial:
    case_id: str
    variant: str
    provider: str
    model_id: str | None
    input_package: bytes


@dataclass(frozen=True, slots=True)
class RealAuthorization:
    manifest_hash: str
    budget_micro_usd: int
    maximum_reserved_micro_usd: int
    operation_caps: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    success: bool
    failure_code: str | None
    usage: ProviderUsage | None
    latency_ms: int
    contract_valid: bool
    evidence_valid: bool
    citation_valid: bool
    source_count: int


@dataclass(frozen=True, slots=True)
class TrialRecord:
    case_id: str
    variant: str
    provider: str
    model_id: str | None
    success: bool
    failure_code: str | None
    contract_valid: bool
    evidence_valid: bool
    citation_valid: bool
    source_count: int
    search_queries: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    tool_use_tokens: int
    modality_tokens: tuple[tuple[str, int], ...]
    provider_cost_micro_usd: int
    total_cost_micro_usd: int
    latency_ms: int


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BenchmarkFailure("invalid_benchmark_manifest") from None


def _read_approved_json(path: Path, approved_hash: str) -> object:
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != approved_hash:
            raise BenchmarkFailure("invalid_benchmark_manifest")
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise BenchmarkFailure("invalid_benchmark_manifest") from None


def load_benchmark_manifest(root: Path, *, raw_manifest: object | None = None) -> BenchmarkManifest:
    """Validate the exact approved corpus and every local fixture before work."""
    root = Path(root)
    approved = _read_approved_json(root / "cases.json", APPROVED_CASES_SHA256)
    fixtures = _read_approved_json(root / "fixtures.json", APPROVED_FIXTURES_SHA256)
    data = approved if raw_manifest is None else raw_manifest
    if _json_bytes(data) != _json_bytes(approved):
        raise BenchmarkFailure("invalid_benchmark_manifest")
    if type(data) is not dict or set(data) != {"manifest_version", "models", "cases"}:
        raise BenchmarkFailure("invalid_benchmark_manifest")
    if data["manifest_version"] != 1 or data["models"] != list(MODEL_IDS):
        raise BenchmarkFailure("invalid_benchmark_manifest")
    cases = data["cases"]
    if type(cases) is not list or len(cases) != 32:
        raise BenchmarkFailure("invalid_benchmark_manifest")
    if type(fixtures) is not dict or set(fixtures) != {"manifest_version", "fixtures"} or fixtures["manifest_version"] != 1 or type(fixtures["fixtures"]) is not list:
        raise BenchmarkFailure("invalid_benchmark_manifest")
    if len(fixtures["fixtures"]) != 16:
        raise BenchmarkFailure("invalid_benchmark_manifest")
    expected_ids = [f"{prefix}-{index:02d}" for prefix, _ in PREFIX_VARIANTS for index in range(1, 5)]
    actual_ids = [case.get("case_id") if type(case) is dict else None for case in cases]
    if actual_ids != expected_ids:
        raise BenchmarkFailure("invalid_benchmark_manifest")
    fixture_ids: set[str] = set()
    total_bytes = 0
    for entry in fixtures["fixtures"]:
        if type(entry) is not dict or set(entry) != {
            "fixture_id", "path", "byte_size", "sha256", "mime_type", "expected_semantic_content"
        }:
            raise BenchmarkFailure("invalid_benchmark_manifest")
        fixture_id, relative = entry["fixture_id"], entry["path"]
        if (type(fixture_id) is not str or fixture_id in fixture_ids or fixture_id not in expected_ids[16:]
                or type(relative) is not str or not relative.startswith("fixtures/")
                or "\\" in relative or ".." in Path(relative).parts):
            raise BenchmarkFailure("invalid_benchmark_manifest")
        fixture_ids.add(fixture_id)
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise BenchmarkFailure("invalid_benchmark_manifest")
        try:
            raw = path.read_bytes()
        except OSError:
            raise BenchmarkFailure("invalid_benchmark_manifest") from None
        if (type(entry["byte_size"]) is not int or len(raw) != entry["byte_size"]
                or hashlib.sha256(raw).hexdigest() != entry["sha256"]):
            raise BenchmarkFailure("invalid_benchmark_manifest")
        total_bytes += len(raw)
    if fixture_ids != set(expected_ids[16:]) or total_bytes >= 8 * 1024 * 1024:
        raise BenchmarkFailure("invalid_benchmark_manifest")
    owned: list[BenchmarkCase] = []
    for case in cases:
        if type(case) is not dict or set(case) not in ({
            "case_id", "variant", "providers", "models", "request", "oracle", "expected_failure_code"
        }, {
            "case_id", "variant", "providers", "models", "request", "oracle", "expected_failure_code", "fixture"
        }):
            raise BenchmarkFailure("invalid_benchmark_manifest")
        prefix = case["case_id"][:3]
        variant = dict(PREFIX_VARIANTS)[prefix]
        if case["variant"] != variant or case["expected_failure_code"] is not None:
            raise BenchmarkFailure("invalid_benchmark_manifest")
        if prefix in {"RES", "ANS", "SRS"}:
            request = case["request"]
            if type(request) is not dict or request.get("max_results") != 5:
                raise BenchmarkFailure("invalid_benchmark_manifest")
            if prefix != "SRS" and "max_sources" in request:
                raise BenchmarkFailure("invalid_benchmark_manifest")
            if prefix == "SRS" and request.get("max_sources") != 3:
                raise BenchmarkFailure("invalid_benchmark_manifest")
        providers = tuple(case["providers"])
        models = tuple(case["models"])
        if providers != (("exa",) if prefix == "RES" else
                         ("exa", "smartfetch", "gemini") if prefix in {"ANS", "SRS"} else
                         ("smartfetch", "gemini") if prefix == "WEB" else
                         ("gemini",)) or models != (() if prefix == "RES" else MODEL_IDS):
            raise BenchmarkFailure("invalid_benchmark_manifest")
        if prefix in {"IMG", "PDF", "AUD", "VID"}:
            matching = next((entry for entry in fixtures["fixtures"] if entry["fixture_id"] == case["case_id"]), None)
            if matching is None or case.get("fixture") != matching["path"]:
                raise BenchmarkFailure("invalid_benchmark_manifest")
        owned.append(BenchmarkCase(case["case_id"], variant, providers, models, _json_bytes(case)))
    return BenchmarkManifest(root, APPROVED_CASES_SHA256, tuple(owned))


def build_trial_inventory(manifest: BenchmarkManifest) -> tuple[BenchmarkTrial, ...]:
    if type(manifest) is not BenchmarkManifest or manifest.sha256 != APPROVED_CASES_SHA256:
        raise BenchmarkFailure("invalid_benchmark_manifest")
    trials: list[BenchmarkTrial] = []
    for ordinal, case in enumerate(manifest.cases):
        if "exa" in case.providers:
            trials.append(BenchmarkTrial(case.case_id, case.variant, "exa", None, case.input_package))
        if case.models:
            ordered = MODEL_IDS if ordinal % 2 == 0 else MODEL_IDS[::-1]
            for model_id in ordered:
                trials.append(BenchmarkTrial(case.case_id, case.variant, "gemini", model_id, case.input_package))
    if len(trials) != 68:
        raise BenchmarkFailure("invalid_benchmark_manifest")
    return tuple(trials)


def _ascii_budget(value: object) -> int:
    if type(value) is not str or not value or len(value) > 8 or value[0] == "0" or not value.isascii() or not value.isdecimal():
        raise BenchmarkFailure("benchmark_not_authorized")
    number = int(value)
    if not 1 <= number <= MAX_BUDGET:
        raise BenchmarkFailure("benchmark_not_authorized")
    return number


def authorize_real_run(
    manifest: BenchmarkManifest, *, execute_real_providers: bool,
    max_total_cost_microusd: str | None, environment: Mapping[str, str],
    operation_caps: Mapping[str, int],
) -> RealAuthorization:
    if not execute_real_providers:
        raise BenchmarkFailure("benchmark_not_authorized")
    budget = _ascii_budget(max_total_cost_microusd)
    if (_ascii_budget(environment.get("SMARTFETCH_BENCHMARK_APPROVED_BUDGET_MICROUSD")) != budget
            or environment.get("SMARTFETCH_BENCHMARK_APPROVAL") != "V111_32_CASE_REAL"
            or any(type(environment.get(key)) is not str or not environment.get(key)
                   for key in ("EXA_API_KEY", "GEMINI_API_KEY"))
            or environment.get("SMARTFETCH_V111_ENABLED") not in {None, "", "false"}):
        raise BenchmarkFailure("benchmark_not_authorized")
    if type(operation_caps) is not dict or set(operation_caps) != {"exa", *MODEL_IDS}:
        raise BenchmarkFailure("benchmark_not_authorized")
    for cap in operation_caps.values():
        if type(cap) is not int or not 1 <= cap <= MAX_BUDGET:
            raise BenchmarkFailure("benchmark_not_authorized")
    inventory = build_trial_inventory(manifest)
    maximum = sum(operation_caps[trial.model_id or "exa"] for trial in inventory)
    if maximum > budget:
        raise BenchmarkFailure("benchmark_budget_exceeded")
    return RealAuthorization(manifest.sha256, budget, maximum,
                             tuple(sorted(operation_caps.items())))


class BenchmarkLedger:
    """Single-run exact cost reservation; tokens are identity-based and single use."""

    def __init__(self, maximum_micro_usd: int) -> None:
        if type(maximum_micro_usd) is not int or not 1 <= maximum_micro_usd <= MAX_BUDGET:
            raise BenchmarkFailure("benchmark_not_authorized")
        self._maximum = maximum_micro_usd
        self._spent = 0
        self._reserved: dict[object, int] = {}
        self._lock = threading.Lock()

    @property
    def spent_micro_usd(self) -> int:
        with self._lock:
            return self._spent

    def reserve(self, maximum_micro_usd: int) -> object:
        if type(maximum_micro_usd) is not int or not 0 < maximum_micro_usd <= MAX_BUDGET:
            raise BenchmarkFailure("invalid_benchmark_usage")
        with self._lock:
            if self._spent + sum(self._reserved.values()) + maximum_micro_usd > self._maximum:
                raise BenchmarkFailure("benchmark_budget_exceeded")
            token = object()
            self._reserved[token] = maximum_micro_usd
            return token

    def commit(self, token: object, actual_micro_usd: int) -> None:
        with self._lock:
            if token not in self._reserved or type(actual_micro_usd) is not int:
                raise BenchmarkFailure("invalid_benchmark_usage")
            maximum = self._reserved.pop(token)
            if not 0 <= actual_micro_usd <= maximum:
                raise BenchmarkFailure("invalid_benchmark_usage")
            self._spent += actual_micro_usd

    def release(self, token: object) -> None:
        with self._lock:
            if token not in self._reserved:
                raise BenchmarkFailure("invalid_benchmark_usage")
            self._reserved.pop(token)


def _record(trial: BenchmarkTrial, outcome: TrialOutcome) -> TrialRecord:
    if type(outcome) is not TrialOutcome or type(outcome.usage) is not ProviderUsage:
        raise BenchmarkFailure("invalid_benchmark_usage")
    usage = outcome.usage
    if usage.provider != trial.provider or any(type(value) is not bool for value in (
        outcome.success, outcome.contract_valid, outcome.evidence_valid, outcome.citation_valid
    )) or any(type(value) is not int or not 0 <= value <= 1_000_000_000 for value in (
        outcome.latency_ms, outcome.source_count
    )):
        raise BenchmarkFailure("invalid_benchmark_result")
    success = outcome.success and outcome.contract_valid and outcome.evidence_valid and outcome.citation_valid
    if success and outcome.failure_code is not None:
        raise BenchmarkFailure("invalid_benchmark_result")
    code = outcome.failure_code if not success else None
    if not success and code not in FAILURE_CODES:
        raise BenchmarkFailure("invalid_benchmark_result")
    return TrialRecord(
        trial.case_id, trial.variant, trial.provider, trial.model_id, success, code,
        outcome.contract_valid, outcome.evidence_valid, outcome.citation_valid,
        outcome.source_count, usage.search_queries, usage.input_tokens,
        usage.output_tokens, usage.thinking_tokens, usage.tool_use_tokens,
        usage.modality_tokens, usage.cost_micro_usd, usage.cost_micro_usd, outcome.latency_ms,
    )


def run_injected_trials(
    manifest: BenchmarkManifest, *, execute_real_providers: bool,
    authorization: RealAuthorization | None = None,
    environment: Mapping[str, str] | None = None,
    executor: Callable[[BenchmarkTrial], TrialOutcome] | None = None,
) -> tuple[TrialRecord, ...]:
    """Offline defaults to validation only; live work requires injection and approval."""
    if not execute_real_providers:
        return ()
    if type(authorization) is not RealAuthorization or authorization.manifest_hash != manifest.sha256 or executor is None:
        raise BenchmarkFailure("benchmark_not_authorized")
    caps = dict(authorization.operation_caps)
    inventory = build_trial_inventory(manifest)
    if (len(authorization.operation_caps) != 3 or set(caps) != {"exa", *MODEL_IDS}
            or any(type(cap) is not int or not 1 <= cap <= MAX_BUDGET for cap in caps.values())
            or type(authorization.budget_micro_usd) is not int
            or not 1 <= authorization.budget_micro_usd <= MAX_BUDGET
            or authorization.maximum_reserved_micro_usd != sum(caps[trial.model_id or "exa"] for trial in inventory)
            or authorization.maximum_reserved_micro_usd > authorization.budget_micro_usd):
        raise BenchmarkFailure("benchmark_not_authorized")
    ledger = BenchmarkLedger(authorization.budget_micro_usd)
    records: list[TrialRecord] = []
    for trial in inventory:
        token = ledger.reserve(caps[trial.model_id or "exa"])
        try:
            outcome = executor(trial)
            record = _record(trial, outcome)
            ledger.commit(token, record.provider_cost_micro_usd)
            records.append(record)
        except BenchmarkFailure:
            try:
                ledger.release(token)
            except BenchmarkFailure:
                pass
            raise
        except Exception:
            try:
                ledger.release(token)
            except BenchmarkFailure:
                pass
            raise BenchmarkFailure("invalid_benchmark_result") from None
    return tuple(records)


def render_reports(manifest: BenchmarkManifest, records: tuple[TrialRecord, ...]) -> tuple[str, str]:
    if type(records) is not tuple or len(records) > 68 or any(type(record) is not TrialRecord for record in records):
        raise BenchmarkFailure("invalid_benchmark_result")
    inventory = build_trial_inventory(manifest)
    if len(records) != len(inventory):
        raise BenchmarkFailure("invalid_benchmark_result")
    rows: list[dict[str, object]] = []
    for record, trial in zip(records, inventory):
        if (record.case_id, record.variant, record.provider, record.model_id) != (
            trial.case_id, trial.variant, trial.provider, trial.model_id
        ):
            raise BenchmarkFailure("invalid_benchmark_result")
        if (any(type(value) is not bool for value in (
                record.success, record.contract_valid, record.evidence_valid, record.citation_valid
            )) or any(type(value) is not int or not 0 <= value <= 100_000_000 for value in (
                record.source_count, record.search_queries, record.input_tokens,
                record.output_tokens, record.thinking_tokens, record.tool_use_tokens,
                record.provider_cost_micro_usd, record.total_cost_micro_usd, record.latency_ms
            )) or record.failure_code not in (None, *FAILURE_CODES)
                or record.total_cost_micro_usd != record.provider_cost_micro_usd
                or (record.success and (record.failure_code is not None or not (
                    record.contract_valid and record.evidence_valid and record.citation_valid
                ))) or (not record.success and record.failure_code is None)
                or type(record.modality_tokens) is not tuple
                or any(type(pair) is not tuple or len(pair) != 2 or
                       type(pair[0]) is not str or pair[0] not in {"text", "image", "pdf", "audio", "video"}
                       or type(pair[1]) is not int or not 0 <= pair[1] <= 100_000_000
                       for pair in record.modality_tokens)):
            raise BenchmarkFailure("invalid_benchmark_result")
        rows.append({
            "case_id": record.case_id, "variant": record.variant,
            "provider": record.provider, "model_id": record.model_id,
            "success": record.success, "failure_code": record.failure_code,
            "contract_valid": record.contract_valid, "evidence_valid": record.evidence_valid,
            "citation_valid": record.citation_valid, "source_count": record.source_count,
            "search_queries": record.search_queries, "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens, "thinking_tokens": record.thinking_tokens,
            "tool_use_tokens": record.tool_use_tokens, "modality_tokens": record.modality_tokens,
            "provider_cost_micro_usd": record.provider_cost_micro_usd,
            "total_cost_micro_usd": record.total_cost_micro_usd,
            "latency_ms": record.latency_ms,
        })
    summaries: dict[str, dict[str, int | None]] = {}
    for _, variant in PREFIX_VARIANTS:
        selected = [record for record in records if record.variant == variant]
        costs = [record.total_cost_micro_usd for record in selected]
        latencies = [record.latency_ms for record in selected]
        successes = sum(record.success for record in selected)
        total = sum(costs)
        summaries[variant] = {
            "trial_count": len(selected), "success_count": successes,
            "provider_cost_micro_usd": total, "median_cost_micro_usd": median_low(costs),
            "maximum_cost_micro_usd": max(costs), "median_latency_ms": median_low(latencies),
            "maximum_latency_ms": max(latencies),
            "failure_adjusted_cost_per_success_micro_usd": (total + successes - 1) // successes if successes else None,
        }
    summary = {"trial_count": len(records), "success_count": sum(record.success for record in records),
               "total_cost_micro_usd": sum(record.total_cost_micro_usd for record in records),
               "variants": summaries}
    report = {"manifest_version": 1, "manifest_hash": manifest.sha256,
              "trials": rows, "summary": summary}
    json_text = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    markdown = "# V1.11 benchmark observations\n\n" + f"Manifest: `{manifest.sha256}`\n\n" + (
        "| Variant | Trials | Successes | Total provider micro-USD | Observed max micro-USD | Median micro-USD |\n"
        "|---|---:|---:|---:|---:|---:|\n"
    ) + "".join(
        f"| {variant} | {item['trial_count']} | {item['success_count']} | "
        f"{item['provider_cost_micro_usd']} | {item['maximum_cost_micro_usd']} | "
        f"{item['median_cost_micro_usd']} |\n"
        for variant, item in summaries.items()
    )
    if len(json_text.encode("utf-8")) > MAX_REPORT_BYTES or len(markdown.encode("utf-8")) > MAX_REPORT_BYTES:
        raise BenchmarkFailure("benchmark_report_too_large")
    return json_text, markdown


__all__ = [
    "BenchmarkFailure", "BenchmarkLedger", "BenchmarkManifest", "BenchmarkTrial",
    "TrialOutcome", "authorize_real_run", "build_trial_inventory",
    "load_benchmark_manifest", "render_reports", "run_injected_trials",
]
