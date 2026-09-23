"""Offline-default V1.11 corpus validation; live provider execution is not wired."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smartfetch.benchmark_v111 import (
    BenchmarkFailure,
    authorize_real_run,
    build_trial_inventory,
    load_benchmark_manifest,
)


ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "v111"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the fixed V1.11 benchmark corpus offline")
    parser.add_argument("--execute-real-providers", action="store_true")
    parser.add_argument("--max-total-cost-microusd")
    parser.add_argument("--exa-max-operation-microusd")
    parser.add_argument("--flash-max-operation-microusd")
    parser.add_argument("--lite-max-operation-microusd")
    args = parser.parse_args(argv)
    try:
        manifest = load_benchmark_manifest(ROOT)
        trials = build_trial_inventory(manifest)
    except BenchmarkFailure as error:
        print(str(error))
        return 2
    if not args.execute_real_providers:
        print(json.dumps({"mode": "offline_validation", "manifest_hash": manifest.sha256,
                          "case_count": len(manifest.cases), "provider_trial_count": len(trials)},
                         sort_keys=True))
        return 0
    cap_args = (
        args.exa_max_operation_microusd,
        args.flash_max_operation_microusd,
        args.lite_max_operation_microusd,
    )
    if args.max_total_cost_microusd is None or any(
        value is None or not value.isascii() or not value.isdecimal() or not 1 <= len(value) <= 8
        for value in cap_args
    ):
        print("benchmark_not_authorized")
        return 2
    caps = {
        "exa": int(cap_args[0]),
        "gemini-3.8-flash": int(cap_args[1]),
        "gemini-3.5-flash-lite": int(cap_args[2]),
    }
    try:
        authorize_real_run(
            manifest, execute_real_providers=True,
            max_total_cost_microusd=args.max_total_cost_microusd,
            environment=os.environ, operation_caps=caps,
        )
    except BenchmarkFailure as error:
        print(str(error))
        return 2
    # No provider adapter or credential value is constructed, printed, or used here.
    # A separately reviewed executor and paid authorization are prerequisites.
    print("real_provider_executor_unavailable")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
