"""Observability and telemetry tracking for SmartFetch retrieval."""

from collections import deque
from dataclasses import asdict, dataclass, field
import threading
import time
from typing import Any


@dataclass
class ObservabilityRecord:
    retrieval_tier: str = "http"  # "cache" | "http" | "browser"
    cache_hit: bool = False
    http_latency_ms: float = 0.0
    browser_wait_ms: float = 0.0
    browser_latency_ms: float = 0.0
    escalation_reason: str = "none"  # "none" | "spa_root_tag" | "noscript_warning" | "minimal_dom" | "manual_override" | "http_error"
    failure_reason: str | None = None
    is_ssrf_rejection: bool = False
    resource_usage: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TelemetryRegistry:
    """Thread-safe registry recording retrieval operations for monitoring and diagnostics."""

    def __init__(self, max_history: int = 1000) -> None:
        self._lock = threading.Lock()
        self._max_history = max_history
        self._history: deque[ObservabilityRecord] = deque(maxlen=max_history)
        self._counters = {
            "total_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "tier_cache": 0,
            "tier_http": 0,
            "tier_browser": 0,
            "tier_http_success": 0,
            "tier_http_failure": 0,
            "tier_browser_success": 0,
            "tier_browser_failure": 0,
            "escalations": 0,
            "ssrf_rejections": 0,
            "failures": 0,
        }
        self._escalation_reasons = {
            "spa_root_tag": 0,
            "noscript_warning": 0,
            "minimal_dom": 0,
            "http_error": 0,
            "manual_override": 0,
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

            if record.retrieval_tier == "http":
                if record.failure_reason is None:
                    self._counters["tier_http_success"] += 1
                else:
                    self._counters["tier_http_failure"] += 1
            elif record.retrieval_tier == "browser":
                if record.failure_reason is None:
                    self._counters["tier_browser_success"] += 1
                else:
                    self._counters["tier_browser_failure"] += 1

            if record.is_ssrf_rejection:
                self._counters["ssrf_rejections"] += 1

            if record.escalation_reason != "none":
                self._counters["escalations"] += 1
                if record.escalation_reason in self._escalation_reasons:
                    self._escalation_reasons[record.escalation_reason] += 1

            if record.failure_reason is not None:
                self._counters["failures"] += 1

            self._history.append(record)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._counters["total_requests"]
            cache_hits = self._counters["cache_hits"]
            cache_hit_rate = round(cache_hits / total, 4) if total > 0 else 0.0

            http_total = self._counters["tier_http_success"] + self._counters["tier_http_failure"]
            http_success_rate = round(self._counters["tier_http_success"] / http_total, 4) if http_total > 0 else 0.0

            browser_total = self._counters["tier_browser_success"] + self._counters["tier_browser_failure"]
            browser_failure_rate = round(self._counters["tier_browser_failure"] / browser_total, 4) if browser_total > 0 else 0.0

            escalation_rate = round(self._counters["escalations"] / total, 4) if total > 0 else 0.0

            from smartfetch.browser_pool import _ACTIVE_POOL
            pool_status = _ACTIVE_POOL.get_status() if _ACTIVE_POOL is not None else None

            return {
                "counters": dict(self._counters),
                "escalation_reasons": dict(self._escalation_reasons),
                "rates": {
                    "cache_hit_rate": cache_hit_rate,
                    "http_success_rate": http_success_rate,
                    "browser_escalation_rate": escalation_rate,
                    "browser_failure_rate": browser_failure_rate,
                },
                "pool_status": pool_status,
                "recent_count": len(self._history),
                "last_record": self._history[-1].to_dict() if self._history else None,
            }

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            for k in self._counters:
                self._counters[k] = 0
            for k in self._escalation_reasons:
                self._escalation_reasons[k] = 0


_REGISTRY = TelemetryRegistry()


def get_telemetry_registry() -> TelemetryRegistry:
    return _REGISTRY


def record_retrieval(record: ObservabilityRecord) -> None:
    _REGISTRY.record(record)


def get_telemetry_snapshot() -> dict[str, Any]:
    return _REGISTRY.snapshot()


def reset_telemetry() -> None:
    _REGISTRY.clear()
