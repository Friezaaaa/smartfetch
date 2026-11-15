"""Fail-closed client for isolated VPS browser worker.

Handles mutual TLS (mTLS), canonical HMAC-SHA256 request signing,
timestamp/nonce validation, and a circuit breaker for remote rendering.
Fails closed with structured diagnostics whenever the worker is unconfigured,
unreachable, or unhealthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from .diagnostics import (
    RetrievalDiagnostics,
    RetrievalFailure,
    attach_diagnostics,
    make_diagnostics,
)
from .security import DNSResolutionError, validate_public_url

_LOGGER = logging.getLogger("smartfetch.remote_browser")

# Canonical error codes for remote browser operations
ERROR_BROWSER_UNAVAILABLE = "browser_unavailable"
ERROR_TIMEOUT = "timeout"
ERROR_POLICY_REJECTION = "policy_rejection"
ERROR_RENDER_FAILED = "render_failed"
ERROR_INVALID_REQUEST = "invalid_request"

VALID_ERROR_CODES = frozenset({
    ERROR_BROWSER_UNAVAILABLE,
    ERROR_TIMEOUT,
    ERROR_POLICY_REJECTION,
    ERROR_RENDER_FAILED,
    ERROR_INVALID_REQUEST,
})


class RemoteBrowserError(RuntimeError):
    """Structured error raised on remote browser rendering failures."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "browser_start",
        upstream_status: int | None = None,
        diagnostics: dict[str, Any] | None = None,
    ):
        self.code = code if code in VALID_ERROR_CODES else ERROR_RENDER_FAILED
        self.message = message
        self.phase = phase
        self.upstream_status = upstream_status
        self.diagnostics = diagnostics or {}
        super().__init__(f"[{self.code}] {self.message}")


@dataclass
class CircuitBreakerState:
    """Thread-safe circuit breaker tracking consecutive worker failures."""

    failure_threshold: int = 3
    recovery_timeout: float = 30.0
    _consecutive_failures: int = 0
    _last_failure_time: float = 0.0
    _state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._state == "OPEN":
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = "HALF_OPEN"
                    return False
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = "CLOSED"

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()
            if self._consecutive_failures >= self.failure_threshold:
                self._state = "OPEN"

    @property
    def current_state(self) -> str:
        with self._lock:
            if self._state == "OPEN" and time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                return "HALF_OPEN"
            return self._state


@dataclass(frozen=True)
class RemoteBrowserConfig:
    """Configuration for connecting to the remote VPS browser worker."""

    worker_url: str | None = None
    hmac_key: str | None = None
    ca_cert_path: str | None = None
    client_cert_path: str | None = None
    client_key_path: str | None = None
    connect_timeout: float = 3.0
    render_timeout: float = 18.0
    failure_threshold: int = 3
    recovery_timeout: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RemoteBrowserConfig:
        environ = os.environ if env is None else env
        worker_url = environ.get("BROWSER_WORKER_URL", "").strip() or None
        hmac_key = environ.get("BROWSER_WORKER_HMAC_KEY", "").strip() or None
        ca_cert = environ.get("BROWSER_WORKER_CA_CERT", "").strip() or None
        client_cert = environ.get("BROWSER_WORKER_CLIENT_CERT", "").strip() or None
        client_key = environ.get("BROWSER_WORKER_CLIENT_KEY", "").strip() or None

        return cls(
            worker_url=worker_url,
            hmac_key=hmac_key,
            ca_cert_path=ca_cert,
            client_cert_path=client_cert,
            client_key_path=client_key,
            connect_timeout=float(environ.get("BROWSER_WORKER_CONNECT_TIMEOUT", "3.0")),
            render_timeout=float(environ.get("BROWSER_WORKER_RENDER_TIMEOUT", "18.0")),
            failure_threshold=int(environ.get("BROWSER_WORKER_FAILURE_THRESHOLD", "3")),
            recovery_timeout=float(environ.get("BROWSER_WORKER_RECOVERY_TIMEOUT", "30.0")),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.worker_url and self.hmac_key)


def compute_canonical_string(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
) -> str:
    """Build canonical representation for HMAC-SHA256 signature."""
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_sha256}"


def sign_request(
    method: str,
    path: str,
    body_bytes: bytes,
    hmac_key: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    """Generate authentication headers including HMAC signature, timestamp, and nonce."""
    ts_str = str(int(time.time()) if timestamp is None else timestamp)
    nonce_str = secrets.token_hex(16) if nonce is None else nonce
    req_id = secrets.token_hex(8) if request_id is None else request_id
    body_hash = hashlib.sha256(body_bytes).hexdigest()

    canonical = compute_canonical_string(method, path, ts_str, nonce_str, body_hash)
    signature = hmac.new(
        hmac_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Content-Type": "application/json",
        "X-SmartFetch-Timestamp": ts_str,
        "X-SmartFetch-Nonce": nonce_str,
        "X-SmartFetch-Signature": signature,
        "X-SmartFetch-Request-Id": req_id,
    }


def verify_signature(
    method: str,
    path: str,
    body_bytes: bytes,
    hmac_key: str,
    headers: Mapping[str, str],
    *,
    max_skew_seconds: int = 30,
) -> bool:
    """Verify incoming HMAC-SHA256 signature and timestamp validity."""
    ts_str = headers.get("X-SmartFetch-Timestamp") or headers.get("x-smartfetch-timestamp")
    nonce = headers.get("X-SmartFetch-Nonce") or headers.get("x-smartfetch-nonce")
    signature = headers.get("X-SmartFetch-Signature") or headers.get("x-smartfetch-signature")

    if not ts_str or not nonce or not signature:
        return False

    try:
        ts = int(ts_str)
    except ValueError:
        return False

    # Check clock skew
    if abs(time.time() - ts) > max_skew_seconds:
        return False

    body_hash = hashlib.sha256(body_bytes).hexdigest()
    canonical = compute_canonical_string(method, path, ts_str, nonce, body_hash)
    expected = hmac.new(
        hmac_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


class RemoteBrowserClient:
    """Thread-safe client that dispatches rendering tasks to the isolated VPS worker."""

    def __init__(
        self,
        config: RemoteBrowserConfig | None = None,
        *,
        session: requests.Session | None = None,
    ):
        self.config = config or RemoteBrowserConfig.from_env()
        self.circuit_breaker = CircuitBreakerState(
            failure_threshold=self.config.failure_threshold,
            recovery_timeout=self.config.recovery_timeout,
        )
        self._session = session or requests.Session()
        self._session.trust_env = False

    @property
    def is_available(self) -> bool:
        """Return True only if configured and circuit breaker is not tripped."""
        if not self.config.is_configured:
            return False
        return not self.circuit_breaker.is_open

    def render(
        self,
        url: str,
        *,
        timeout: float = 15.0,
        max_chars: int = 20000,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Render a target URL on the isolated worker, failing closed on any boundary error.

        Returns a dictionary conforming to SmartFetch's render contract:
        {
            "success": True,
            "requested_url": str,
            "final_url": str,
            "status_code": int,
            "render_method": "browser",
            "elapsed_ms": int,
            "content": str,
            "html": str,
            "markdown": str,
            ...
        }
        """
        # 1. Fail closed if not configured
        if not self.config.is_configured:
            raise RemoteBrowserError(
                ERROR_BROWSER_UNAVAILABLE,
                "Remote browser worker is not configured",
                phase="validate",
            )

        # 2. Check circuit breaker
        if self.circuit_breaker.is_open:
            raise RemoteBrowserError(
                ERROR_BROWSER_UNAVAILABLE,
                "Remote browser worker circuit breaker is OPEN",
                phase="capacity",
            )

        # 3. Validate target URL defensively before sending over the wire
        try:
            safe_url = validate_public_url(url)
        except DNSResolutionError as exc:
            raise RemoteBrowserError(
                ERROR_POLICY_REJECTION,
                f"DNS resolution failed: {exc}",
                phase="dns",
            ) from exc
        except ValueError as exc:
            raise RemoteBrowserError(
                ERROR_POLICY_REJECTION,
                f"URL policy rejection: {exc}",
                phase="validate",
            ) from exc

        # 4. Construct request payload
        render_timeout = min(timeout, self.config.render_timeout)
        payload = {
            "url": safe_url,
            "timeout_seconds": render_timeout,
            "max_chars": max_chars,
        }
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        base_url = self.config.worker_url.rstrip("/")
        endpoint_path = "/v1/render"
        full_url = f"{base_url}{endpoint_path}"

        headers = sign_request(
            "POST",
            endpoint_path,
            payload_bytes,
            self.config.hmac_key,
            request_id=request_id,
        )

        # 5. Configure TLS verification and client certificate if provided
        verify: bool | str = True
        if self.config.ca_cert_path and os.path.isfile(self.config.ca_cert_path):
            verify = self.config.ca_cert_path

        cert = None
        if self.config.client_cert_path and self.config.client_key_path:
            cert = (self.config.client_cert_path, self.config.client_key_path)

        started = time.monotonic()
        try:
            response = self._session.post(
                full_url,
                data=payload_bytes,
                headers=headers,
                timeout=(self.config.connect_timeout, render_timeout + 3.0),
                verify=verify,
                cert=cert,
            )
        except requests.exceptions.Timeout as exc:
            self.circuit_breaker.record_failure()
            raise RemoteBrowserError(
                ERROR_TIMEOUT,
                f"Remote browser request timed out after {render_timeout}s",
                phase="browser_navigate",
            ) from exc
        except requests.exceptions.SSLError as exc:
            self.circuit_breaker.record_failure()
            raise RemoteBrowserError(
                ERROR_BROWSER_UNAVAILABLE,
                f"mTLS handshake failure with browser worker: {exc}",
                phase="tls",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            self.circuit_breaker.record_failure()
            raise RemoteBrowserError(
                ERROR_BROWSER_UNAVAILABLE,
                f"Connection error contacting browser worker: {exc}",
                phase="connect",
            ) from exc
        except Exception as exc:
            self.circuit_breaker.record_failure()
            raise RemoteBrowserError(
                ERROR_RENDER_FAILED,
                f"Unexpected transport failure: {exc}",
                phase="browser_start",
            ) from exc

        elapsed_ms = round((time.monotonic() - started) * 1000)

        # 6. Parse response envelope
        return self._handle_response(response, elapsed_ms, safe_url)

    def _handle_response(
        self,
        response: requests.Response,
        elapsed_ms: int,
        requested_url: str,
    ) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            self.circuit_breaker.record_failure()
            raise RemoteBrowserError(
                ERROR_RENDER_FAILED,
                f"Worker returned non-JSON response (HTTP {response.status_code})",
                phase="response",
                upstream_status=response.status_code,
            )

        if not isinstance(data, dict):
            self.circuit_breaker.record_failure()
            raise RemoteBrowserError(
                ERROR_RENDER_FAILED,
                "Worker returned invalid JSON envelope",
                phase="response",
            )

        # If HTTP response status is successful (200) and payload says success
        if response.status_code == 200 and data.get("success") is True:
            self.circuit_breaker.record_success()
            data.setdefault("elapsed_ms", elapsed_ms)
            data.setdefault("requested_url", requested_url)
            data.setdefault("render_method", "browser")
            return data

        # Map error response
        error_code = data.get("error_code")
        error_msg = data.get("error", f"Remote worker error (HTTP {response.status_code})")
        phase = data.get("phase", "response")
        upstream_status = data.get("upstream_status")

        # Record circuit breaker failure only for server/infrastructure failures
        if response.status_code >= 500 or error_code in (ERROR_BROWSER_UNAVAILABLE, ERROR_RENDER_FAILED):
            self.circuit_breaker.record_failure()

        raise RemoteBrowserError(
            error_code if error_code in VALID_ERROR_CODES else ERROR_RENDER_FAILED,
            error_msg,
            phase=phase,
            upstream_status=upstream_status,
            diagnostics=data.get("diagnostics"),
        )
