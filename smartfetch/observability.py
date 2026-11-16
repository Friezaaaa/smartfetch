"""Observability and telemetry tracking for SmartFetch retrieval."""

from dataclasses import asdict, dataclass, field
import threading
import time
from typing import Any


@dataclass
class ObservabilityRecord:
    retrieval_tier: str = "http"  # "cache" | "http" | "browser"
    cache_hit: bool = False
    http_latency_ms: float = 0.0
    browser_latency_ms: float = 0.0
    escalation_reason: str = "none"  # "none" | "spa_root_tag" | "noscript_warning" | "minimal_dom" | "manual_override" | "http_error"
    failure_reason: str | None = None
    resource_usage: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TelemetryRegistry:
    """Thread-safe registry recording retrieval operations for monitoring and diagnostics."""

    def __init__(self, max_history: int = 1000) -> None:
        self._lock = threading.Lock()
        self._max_history = max_history
        self._history: list[ObservabilityRecord] = []
        self._counters = {
            "total_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "tier_cache": 0,
            "tier_http": 0,
            "tier_browser": 0,
            "escalations": 0,
            "failures": 0,
        }

    def record(self, record: ObservabilityRecord) -> None:
        with self._lock:
            self._counters["total_requests"] += 1
            if record.cache_hit:
                self._counters["cache_hits"] += 1
            else:
                self._counters["cache_misses"] += 1

            tier_key = f"tier_{record.retrieval_tier}"
            if tier_key in self._counters:
                self._counters[tier_key] += 1

            if record.escalation_reason != "none":
                self._counters["escalations"] += 1

            if record.failure_reason is not None:
                self._counters["failures"] += 1

            self._history.append(record)
            if len(self._history) > self._max_history:
                self._history.pop(0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "recent_count": len(self._history),
                "last_record": self._history[-1].to_dict() if self._history else None,
            }

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            for k in self._counters:
                self._counters[k] = 0


_REGISTRY = TelemetryRegistry()


def get_telemetry_registry() -> TelemetryRegistry:
    return _REGISTRY


def record_retrieval(record: ObservabilityRecord) -> None:
    _REGISTRY.record(record)


def get_telemetry_snapshot() -> dict[str, Any]:
    return _REGISTRY.snapshot()


def reset_telemetry() -> None:
    _REGISTRY.clear()
