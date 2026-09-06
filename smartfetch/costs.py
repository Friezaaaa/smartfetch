"""Bounded provider usage and cost value types for later V1.11 adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProviderName = Literal["exa", "gemini"]
ModalityName = Literal["text", "image", "pdf", "audio", "video"]
MAX_USAGE_COUNT = 100_000_000
MAX_PROVIDER_COST_MICRO_USD = 1_000_000_000


class UsageAccountingError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_provider_usage")


def _bounded_count(value: object, *, maximum: int = MAX_USAGE_COUNT) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    provider: ProviderName
    search_queries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    tool_use_tokens: int = 0
    modality_tokens: tuple[tuple[ModalityName, int], ...] = ()
    cost_micro_usd: int = 0

    def __post_init__(self) -> None:
        if self.provider not in {"exa", "gemini"}:
            raise UsageAccountingError()
        for count in (
            self.search_queries,
            self.input_tokens,
            self.output_tokens,
            self.thinking_tokens,
            self.tool_use_tokens,
        ):
            if not _bounded_count(count):
                raise UsageAccountingError()
        if not _bounded_count(self.cost_micro_usd, maximum=MAX_PROVIDER_COST_MICRO_USD):
            raise UsageAccountingError()
        if type(self.modality_tokens) is not tuple:
            raise UsageAccountingError()
        allowed_modalities = {"text", "image", "pdf", "audio", "video"}
        seen: set[str] = set()
        for entry in self.modality_tokens:
            if type(entry) is not tuple or len(entry) != 2:
                raise UsageAccountingError()
            modality, count = entry
            if (
                not isinstance(modality, str)
                or modality not in allowed_modalities
                or modality in seen
                or not _bounded_count(count)
            ):
                raise UsageAccountingError()
            seen.add(modality)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens + self.tool_use_tokens


__all__ = [
    "MAX_PROVIDER_COST_MICRO_USD",
    "MAX_USAGE_COUNT",
    "ModalityName",
    "ProviderName",
    "ProviderUsage",
    "UsageAccountingError",
]
