from decimal import Decimal
import threading
import unittest

from smartfetch.provider_controls import (
    RequestCostBudget,
    exa_cost_micro_usd,
    gemini_usage,
)
from smartfetch.provider_health import ProviderAdapterError


class ProviderControlTests(unittest.TestCase):
    def test_budget_reserves_commits_and_releases_exact_integer_micro_usd(self) -> None:
        budget = RequestCostBudget(max_total_micro_usd=20_000)
        first = budget.reserve(provider="exa", maximum_micro_usd=12_000)
        self.assertEqual(budget.reserved_micro_usd, 12_000)
        budget.commit(first, actual_micro_usd=9_000)
        self.assertEqual(budget.spent_micro_usd, 9_000)
        self.assertEqual(budget.reserved_micro_usd, 0)

        second = budget.reserve(provider="gemini", maximum_micro_usd=11_000)
        budget.release(second)
        self.assertEqual(budget.spent_micro_usd, 9_000)
        self.assertEqual(budget.reserved_micro_usd, 0)

    def test_budget_prevents_concurrent_overspend(self) -> None:
        budget = RequestCostBudget(max_total_micro_usd=10_000)
        barrier = threading.Barrier(6)
        outcomes: list[str] = []
        lock = threading.Lock()

        def reserve() -> None:
            barrier.wait()
            try:
                budget.reserve(provider="gemini", maximum_micro_usd=6_000)
            except ProviderAdapterError as exc:
                outcome = exc.code
            else:
                outcome = "reserved"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=reserve) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("reserved"), 1)
        self.assertEqual(outcomes.count("capacity_unavailable"), 5)

    def test_budget_rejects_invalid_and_over_reservation_actuals(self) -> None:
        for value in (True, -1, 1.5, 1_000_000_001):
            with self.assertRaisesRegex(ValueError, "invalid_provider_budget"):
                RequestCostBudget(max_total_micro_usd=value)
        budget = RequestCostBudget(max_total_micro_usd=100)
        reservation = budget.reserve(provider="exa", maximum_micro_usd=100)
        with self.assertRaisesRegex(ProviderAdapterError, "capacity_unavailable"):
            budget.commit(reservation, actual_micro_usd=101)
        self.assertEqual(budget.spent_micro_usd, 0)
        self.assertEqual(budget.reserved_micro_usd, 0)

    def test_exa_decimal_cost_rounds_up_without_binary_float(self) -> None:
        self.assertEqual(exa_cost_micro_usd(Decimal("0.007")), 7_000)
        self.assertEqual(exa_cost_micro_usd(Decimal("0.0000001")), 1)
        for value in (0.007, Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1"), "0.007"):
            with self.assertRaisesRegex(ValueError, "invalid_provider_usage"):
                exa_cost_micro_usd(value)

    def test_gemini_costs_use_exact_rational_rates_and_do_not_double_count_modalities(self) -> None:
        flash = gemini_usage(
            model_id="gemini-3.8-flash",
            input_tokens=3,
            output_tokens=2,
            thinking_tokens=1,
            tool_use_tokens=0,
            modality_tokens=(("text", 3),),
        )
        self.assertEqual(flash.cost_micro_usd, 27)  # 3*1.5 + 3*7.5
        self.assertEqual(flash.total_tokens, 6)

        lite = gemini_usage(
            model_id="gemini-3.5-flash-lite",
            input_tokens=1,
            output_tokens=1,
            thinking_tokens=0,
            tool_use_tokens=0,
            modality_tokens=(("image", 1),),
        )
        self.assertEqual(lite.cost_micro_usd, 3)  # ceil(0.3 + 2.5)

    def test_gemini_usage_rejects_unknown_models_and_malformed_counts(self) -> None:
        base = dict(
            model_id="gemini-3.8-flash",
            input_tokens=1,
            output_tokens=1,
            thinking_tokens=0,
            tool_use_tokens=0,
            modality_tokens=(),
        )
        for key, value in (("model_id", "other"), ("input_tokens", True), ("output_tokens", -1)):
            values = dict(base)
            values[key] = value
            with self.assertRaisesRegex(ValueError, "invalid_provider_usage"):
                gemini_usage(**values)


if __name__ == "__main__":
    unittest.main()
