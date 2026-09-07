"""Fail-closed provider readiness and local circuit state for V1.11."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Callable, Literal


ProviderErrorCode = Literal[
    "provider_unavailable",
    "capacity_unavailable",
    "provider_timeout",
    "search_failed",
    "model_failed",
    "invalid_provider_output",
]
ProviderName = Literal["exa", "gemini"]
CircuitState = Literal["closed", "open", "half_open"]

_ERROR_CODES = {
    "provider_unavailable",
    "capacity_unavailable",
    "provider_timeout",
    "search_failed",
    "model_failed",
    "invalid_provider_output",
}


class ProviderAdapterError(RuntimeError):
    """Finite provider failure that deliberately discards the raw cause."""

    def __init__(
        self,
        code: ProviderErrorCode,
        *,
        provider: ProviderName,
        cause: BaseException | None = None,
    ) -> None:
        del cause
        if type(code) is not str or code not in _ERROR_CODES:
            raise ValueError("invalid_provider_error")
        if type(provider) is not str or provider not in {"exa", "gemini"}:
            raise ValueError("invalid_provider_error")
        self.code = code
        self.provider = provider
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Server-injected provider limits; the credential is excluded from repr."""

    provider: ProviderName
    api_key: str | None = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int
    max_cost_micro_usd: int

    def __post_init__(self) -> None:
        if type(self.provider) is not str or self.provider not in {"exa", "gemini"}:
            raise ValueError("invalid_provider_config")
        if self.api_key is not None and type(self.api_key) is not str:
            raise ValueError("invalid_provider_config")
        if (
            type(self.timeout_seconds) not in {int, float}
            or isinstance(self.timeout_seconds, bool)
            or not 0 < self.timeout_seconds <= 120
        ):
            raise ValueError("invalid_provider_config")
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= 4_194_304:
            raise ValueError("invalid_provider_config")
        if type(self.max_cost_micro_usd) is not int or not 0 <= self.max_cost_micro_usd <= 1_000_000_000:
            raise ValueError("invalid_provider_config")

    @property
    def configured(self) -> bool:
        return type(self.api_key) is str and bool(self.api_key.strip())

    def require_configured(self) -> None:
        if not self.configured:
            raise ProviderAdapterError("provider_unavailable", provider=self.provider)


class ProviderCallPermit:
    __slots__ = ("_owner", "probe")

    def __init__(self, owner: "ProviderCircuitBreaker", *, probe: bool) -> None:
        self._owner = owner
        self.probe = probe


class ProviderCircuitBreaker:
    """Small lock-protected closed/open/half-open provider circuit."""

    def __init__(
        self,
        *,
        provider: ProviderName,
        failure_threshold: int = 1,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(provider) is not str or provider not in {"exa", "gemini"}:
            raise ValueError("invalid_provider_config")
        if type(failure_threshold) is not int or not 1 <= failure_threshold <= 100:
            raise ValueError("invalid_provider_config")
        if (
            type(recovery_seconds) not in {int, float}
            or isinstance(recovery_seconds, bool)
            or not 0 < recovery_seconds <= 3600
            or not callable(clock)
        ):
            raise ValueError("invalid_provider_config")
        self._provider = provider
        self._failure_threshold = failure_threshold
        self._recovery_seconds = float(recovery_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._state: CircuitState = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._probe_active = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def require_available(self) -> None:
        """Readiness check that performs no provider work and reserves no probe."""
        with self._lock:
            if self._state == "open" and self._clock() - self._opened_at < self._recovery_seconds:
                raise ProviderAdapterError("provider_unavailable", provider=self._provider)
            if self._state == "half_open" and self._probe_active:
                raise ProviderAdapterError("provider_unavailable", provider=self._provider)

    def begin_call(self) -> ProviderCallPermit:
        with self._lock:
            if self._state == "open":
                if self._clock() - self._opened_at < self._recovery_seconds:
                    raise ProviderAdapterError("provider_unavailable", provider=self._provider)
                self._state = "half_open"
                self._probe_active = True
                return ProviderCallPermit(self, probe=True)
            if self._state == "half_open":
                raise ProviderAdapterError("provider_unavailable", provider=self._provider)
            return ProviderCallPermit(self, probe=False)

    def record_success(self, permit: ProviderCallPermit) -> None:
        self._require_permit(permit)
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._probe_active = False

    def record_failure(self, permit: ProviderCallPermit) -> None:
        self._require_permit(permit)
        with self._lock:
            self._failures += 1
            if permit.probe or self._failures >= self._failure_threshold:
                self._state = "open"
                self._opened_at = self._clock()
            self._probe_active = False

    def _require_permit(self, permit: ProviderCallPermit) -> None:
        if type(permit) is not ProviderCallPermit or permit._owner is not self:
            raise ValueError("invalid_provider_permit")


__all__ = [
    "ProviderAdapterError",
    "ProviderCallPermit",
    "ProviderCircuitBreaker",
    "ProviderConfig",
    "ProviderErrorCode",
]
