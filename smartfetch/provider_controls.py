"""Exact request-local provider spend controls for V1.11."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
import threading
from typing import Iterable

from .costs import MAX_PROVIDER_COST_MICRO_USD, ProviderUsage, UsageAccountingError
from .provider_health import ProviderAdapterError


_GEMINI_RATES = {
    "gemini-3.8-flash": ((3, 2), (15, 2)),
    "gemini-3.5-flash-lite": ((3, 10), (5, 2)),
}


class CostReservation:
    __slots__ = ("_owner", "provider", "maximum_micro_usd", "_active")

    def __init__(self, owner: "RequestCostBudget", provider: str, maximum_micro_usd: int) -> None:
        self._owner = owner
        self.provider = provider
        self.maximum_micro_usd = maximum_micro_usd
        self._active = True


class RequestCostBudget:
    """A request-owned budget safe for concurrent provider operations."""

    def __init__(self, *, max_total_micro_usd: int) -> None:
        if type(max_total_micro_usd) is not int or not 0 <= max_total_micro_usd <= MAX_PROVIDER_COST_MICRO_USD:
            raise ValueError("invalid_provider_budget")
        self._maximum = max_total_micro_usd
        self._spent = 0
        self._reserved = 0
        self._lock = threading.Lock()

    @property
    def spent_micro_usd(self) -> int:
        with self._lock:
            return self._spent

    @property
    def reserved_micro_usd(self) -> int:
        with self._lock:
            return self._reserved

    def reserve(self, *, provider: str, maximum_micro_usd: int) -> CostReservation:
        if type(provider) is not str or provider not in {"exa", "gemini"}:
            raise ValueError("invalid_provider_budget")
        if type(maximum_micro_usd) is not int or not 0 <= maximum_micro_usd <= MAX_PROVIDER_COST_MICRO_USD:
            raise ValueError("invalid_provider_budget")
        with self._lock:
            if self._spent + self._reserved + maximum_micro_usd > self._maximum:
                raise ProviderAdapterError("capacity_unavailable", provider=provider)
            self._reserved += maximum_micro_usd
            return CostReservation(self, provider, maximum_micro_usd)

    def commit(self, reservation: CostReservation, *, actual_micro_usd: int) -> None:
        with self._lock:
            maximum = self._consume(reservation)
            if (
                type(actual_micro_usd) is not int
                or not 0 <= actual_micro_usd <= maximum
                or self._spent + actual_micro_usd > self._maximum
            ):
                raise ProviderAdapterError("capacity_unavailable", provider=reservation.provider)
            self._spent += actual_micro_usd

    def release(self, reservation: CostReservation) -> None:
        with self._lock:
            self._consume(reservation)

    def _consume(self, reservation: CostReservation) -> int:
        if (
            type(reservation) is not CostReservation
            or reservation._owner is not self
            or not reservation._active
        ):
            raise ValueError("invalid_provider_budget")
        reservation._active = False
        self._reserved -= reservation.maximum_micro_usd
        return reservation.maximum_micro_usd


def exa_cost_micro_usd(cost_dollars: object) -> int:
    """Convert Exa's Decimal dollar total to micro-USD, rounding upward."""
    if type(cost_dollars) is not Decimal or not cost_dollars.is_finite() or cost_dollars < 0:
        raise UsageAccountingError()
    micros = cost_dollars * Decimal(1_000_000)
    integral = int(micros.to_integral_value(rounding="ROUND_CEILING"))
    if not 0 <= integral <= MAX_PROVIDER_COST_MICRO_USD:
        raise UsageAccountingError()
    return integral


def gemini_usage(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    tool_use_tokens: int,
    modality_tokens: Iterable[tuple[str, int]],
) -> ProviderUsage:
    """Build exact January-2027 usage cost without double-counting modalities."""
    if type(model_id) is not str or model_id not in _GEMINI_RATES:
        raise UsageAccountingError()
    counts = (input_tokens, output_tokens, thinking_tokens, tool_use_tokens)
    if any(type(value) is not int or value < 0 for value in counts):
        raise UsageAccountingError()
    if type(modality_tokens) is not tuple:
        raise UsageAccountingError()
    input_rate, output_rate = _GEMINI_RATES[model_id]
    exact_cost = (
        input_tokens * Fraction(*input_rate)
        + (output_tokens + thinking_tokens + tool_use_tokens) * Fraction(*output_rate)
    )
    cost = (exact_cost.numerator + exact_cost.denominator - 1) // exact_cost.denominator
    if cost > MAX_PROVIDER_COST_MICRO_USD:
        raise UsageAccountingError()
    return ProviderUsage(
        provider="gemini",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        tool_use_tokens=tool_use_tokens,
        modality_tokens=modality_tokens,
        cost_micro_usd=cost,
    )


__all__ = [
    "CostReservation",
    "RequestCostBudget",
    "exa_cost_micro_usd",
    "gemini_usage",
]
