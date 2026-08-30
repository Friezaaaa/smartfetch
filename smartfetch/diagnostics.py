"""Typed, privacy-safe retrieval failure diagnostics."""

from dataclasses import dataclass, replace
import ipaddress
import re
from typing import Literal, Optional
from urllib.parse import urlsplit


RetrievalStrategy = Literal['http', 'browser']
RetrievalPhase = Literal[
    'validate',
    'dns',
    'connect',
    'tls',
    'redirect',
    'response',
    'extract',
    'browser_start',
    'browser_navigate',
    'browser_extract',
]
RetrievalFailureCode = Literal[
    'timeout',
    'dns',
    'tls',
    'blocked_response',
    'upstream_status',
    'browser_failure',
    'policy_rejection',
    'invalid_content',
    'unknown',
]

SAFE_STRATEGIES = frozenset(('http', 'browser'))
SAFE_PHASES = frozenset((
    'validate',
    'dns',
    'connect',
    'tls',
    'redirect',
    'response',
    'extract',
    'browser_start',
    'browser_navigate',
    'browser_extract',
))
SAFE_FAILURE_CODES = frozenset((
    'timeout',
    'dns',
    'tls',
    'blocked_response',
    'upstream_status',
    'browser_failure',
    'policy_rejection',
    'invalid_content',
    'unknown',
))
_SAFE_HOST = re.compile(r'^[a-z0-9.-]{1,253}$')


@dataclass(frozen=True)
class RetrievalDiagnostics:
    """Finite diagnostic state safe to pass toward activity logging."""

    target_host: str
    strategy: RetrievalStrategy
    phase: RetrievalPhase
    failure_code: RetrievalFailureCode
    http_attempted: bool
    http_retry_attempted: bool
    browser_attempted: bool
    fallback_attempted: bool
    upstream_status: Optional[int] = None


class RetrievalFailure(RuntimeError):
    """Final retrieval failure carrying typed diagnostics for the API layer."""

    def __init__(self, message: str, diagnostics: RetrievalDiagnostics):
        super().__init__(message)
        self.retrieval_diagnostics = diagnostics


def normalize_target_host(url: object) -> str:
    """Return only a normalized hostname, never URL credentials or components."""
    if not isinstance(url, str):
        return 'unknown'
    try:
        hostname = urlsplit(url).hostname
    except (TypeError, ValueError):
        return 'unknown'
    if not hostname:
        return 'unknown'

    hostname = hostname.rstrip('.').lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return 'ip-literal'

    try:
        hostname = hostname.encode('idna').decode('ascii').lower()
    except (UnicodeError, ValueError):
        return 'unknown'
    return hostname[:253] if _SAFE_HOST.fullmatch(hostname[:253]) else 'unknown'


def safe_diagnostic_host(value: object) -> Optional[str]:
    """Validate an already-normalized diagnostic hostname for log emission."""
    if not isinstance(value, str):
        return None
    if value in {'unknown', 'ip-literal'}:
        return value
    if value != value.lower() or not _SAFE_HOST.fullmatch(value):
        return None
    return value


def make_diagnostics(
    url: object,
    strategy: RetrievalStrategy,
    phase: RetrievalPhase,
    failure_code: RetrievalFailureCode,
    *,
    http_attempted: bool = False,
    http_retry_attempted: bool = False,
    browser_attempted: bool = False,
    fallback_attempted: bool = False,
    upstream_status: Optional[int] = None,
) -> RetrievalDiagnostics:
    """Build a typed diagnostic without retaining the input URL."""
    safe_status = (
        upstream_status
        if isinstance(upstream_status, int)
        and not isinstance(upstream_status, bool)
        and 100 <= upstream_status <= 599
        else None
    )
    return RetrievalDiagnostics(
        target_host=normalize_target_host(url),
        strategy=strategy,
        phase=phase,
        failure_code=failure_code,
        http_attempted=bool(http_attempted),
        http_retry_attempted=bool(http_retry_attempted),
        browser_attempted=bool(browser_attempted),
        fallback_attempted=bool(fallback_attempted),
        upstream_status=safe_status,
    )


def attach_diagnostics(
    error: BaseException,
    diagnostics: RetrievalDiagnostics,
) -> BaseException:
    """Attach diagnostics without changing an exception's public text or type."""
    try:
        error.retrieval_diagnostics = diagnostics
        return error
    except Exception:
        return RetrievalFailure(str(error), diagnostics)


def diagnostics_for_exception(
    error: BaseException,
) -> Optional[RetrievalDiagnostics]:
    """Read only trusted typed diagnostics; never infer from exception text."""
    diagnostics = getattr(error, 'retrieval_diagnostics', None)
    return diagnostics if isinstance(diagnostics, RetrievalDiagnostics) else None


def aggregate_browser_failure(
    url: object,
    browser_error: BaseException,
    http_diagnostics: Optional[RetrievalDiagnostics],
    *,
    force_browser: bool,
) -> RetrievalDiagnostics:
    """Combine attempt flags while retaining the final browser failure boundary."""
    browser = diagnostics_for_exception(browser_error) or make_diagnostics(
        url,
        'browser',
        'browser_extract',
        'unknown',
        browser_attempted=True,
    )
    return replace(
        browser,
        target_host=normalize_target_host(url),
        http_attempted=(
            False if force_browser else (
                http_diagnostics.http_attempted
                if http_diagnostics is not None
                else True
            )
        ),
        http_retry_attempted=(
            False if force_browser or http_diagnostics is None
            else http_diagnostics.http_retry_attempted
        ),
        browser_attempted=browser.browser_attempted,
        fallback_attempted=not force_browser,
    )


def failure_activity_fields(
    error: BaseException,
    url: object,
    force_browser: bool,
) -> dict:
    """Return allowlisted fields without parsing raw exception messages."""
    diagnostics = diagnostics_for_exception(error)
    if diagnostics is None:
        diagnostics = make_diagnostics(
            url,
            'browser',
            'browser_extract',
            'unknown',
            http_attempted=not force_browser,
            browser_attempted=True,
            fallback_attempted=not force_browser,
        )
    fields = {
        'target_host': diagnostics.target_host,
        'strategy': diagnostics.strategy,
        'phase': diagnostics.phase,
        'failure_code': diagnostics.failure_code,
        'http_attempted': diagnostics.http_attempted,
        'http_retry_attempted': diagnostics.http_retry_attempted,
        'browser_attempted': diagnostics.browser_attempted,
        'fallback_attempted': diagnostics.fallback_attempted,
    }
    if diagnostics.upstream_status is not None:
        fields['upstream_status'] = diagnostics.upstream_status
    return fields
