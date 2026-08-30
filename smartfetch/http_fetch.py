import os
import socket
import time
from urllib.parse import urljoin

import requests

from .diagnostics import attach_diagnostics, make_diagnostics
from .security import validate_public_url

MAX_BYTES = int(os.getenv('MAX_RESPONSE_BYTES', '3000000'))
TIMEOUT = float(os.getenv('FETCH_TIMEOUT_SECONDS', '12'))
MAX_REDIRECTS = 5
TRANSIENT_STATUS_CODES = {502, 503, 504}

SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({
    'User-Agent': 'SmartFetch/0.1 (+agent web retrieval prototype)',
    'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1',
    'Accept-Language': 'en-US,en;q=0.8',
})


class _HTTPFetchResult(dict):
    """Mapping-compatible page result with internal retry-attempt state."""

    def __init__(self, *args, retry_attempted: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.retry_attempted = bool(retry_attempted)


def _request_with_retry(url: str):
    for attempt in range(2):
        try:
            response = SESSION.get(
                url,
                allow_redirects=False,
                timeout=TIMEOUT,
                stream=True,
            )
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 0:
                continue
            raise

        if response.status_code in TRANSIENT_STATUS_CODES and attempt == 0:
            response.close()
            continue
        return response, attempt > 0

    raise RuntimeError('Transient request retry exhausted')


def _network_failure(error: BaseException) -> tuple[str, str]:
    if isinstance(error, requests.exceptions.SSLError):
        return 'tls', 'tls'
    if isinstance(error, requests.Timeout):
        return 'connect', 'timeout'
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, socket.gaierror):
            return 'dns', 'dns'
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return 'connect', 'unknown'


def _annotate_and_raise(error: BaseException, diagnostics) -> None:
    annotated = attach_diagnostics(error, diagnostics)
    if annotated is error:
        raise error
    raise annotated from error


def http_fetch(url: str) -> dict:
    try:
        current = validate_public_url(url)
    except Exception as error:
        _annotate_and_raise(error, make_diagnostics(
            url,
            'http',
            'validate',
            'policy_rejection',
        ))

    retry_attempted = False
    for _ in range(MAX_REDIRECTS + 1):
        try:
            response, retried = _request_with_retry(current)
            retry_attempted = retry_attempted or retried
        except Exception as error:
            phase, failure_code = _network_failure(error)
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                phase,
                failure_code,
                http_attempted=True,
                http_retry_attempted=isinstance(
                    error,
                    (requests.ConnectionError, requests.Timeout),
                ),
            ))

        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get('Location')
            if not location:
                error = RuntimeError(
                    f'Redirect {response.status_code} missing Location header'
                )
                _annotate_and_raise(error, make_diagnostics(
                    url,
                    'http',
                    'redirect',
                    'invalid_content',
                    http_attempted=True,
                    http_retry_attempted=retry_attempted,
                    upstream_status=response.status_code,
                ))
            try:
                current = validate_public_url(urljoin(current, location))
            except Exception as error:
                _annotate_and_raise(error, make_diagnostics(
                    url,
                    'http',
                    'redirect',
                    'policy_rejection',
                    http_attempted=True,
                    http_retry_attempted=retry_attempted,
                    upstream_status=response.status_code,
                ))
            continue

        if response.status_code >= 400:
            failure_code = (
                'blocked_response'
                if response.status_code in {401, 403, 407, 429}
                else 'upstream_status'
            )
            error = RuntimeError(f'HTTP status {response.status_code}')
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                'response',
                failure_code,
                http_attempted=True,
                http_retry_attempted=retry_attempted,
                upstream_status=response.status_code,
            ))

        content_type = response.headers.get('Content-Type', '').lower()
        if not any(x in content_type for x in ('text/html', 'application/xhtml+xml', 'text/plain')):
            error = RuntimeError(
                f'Unsupported content-type: {content_type or "unknown"}'
            )
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                'response',
                'invalid_content',
                http_attempted=True,
                http_retry_attempted=retry_attempted,
            ))

        declared = int(response.headers.get('Content-Length') or 0)
        if declared > MAX_BYTES:
            error = RuntimeError(f'Response too large ({declared} bytes)')
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                'response',
                'invalid_content',
                http_attempted=True,
                http_retry_attempted=retry_attempted,
            ))

        chunks = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_BYTES:
                    error = RuntimeError(
                        f'Response exceeded {MAX_BYTES} byte limit'
                    )
                    _annotate_and_raise(error, make_diagnostics(
                        url,
                        'http',
                        'response',
                        'invalid_content',
                        http_attempted=True,
                        http_retry_attempted=retry_attempted,
                    ))
                chunks.append(chunk)
        except Exception as error:
            if getattr(error, 'retrieval_diagnostics', None) is not None:
                raise
            phase, failure_code = _network_failure(error)
            if phase == 'connect':
                phase = 'response'
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                phase,
                failure_code,
                http_attempted=True,
                http_retry_attempted=retry_attempted,
            ))

        raw = b''.join(chunks)
        encoding = response.encoding or 'utf-8'
        try:
            html = raw.decode(encoding, errors='replace')
        except LookupError:
            html = raw.decode('utf-8', errors='replace')

        return _HTTPFetchResult({
            'html': html,
            'final_url': current,
            'status_code': response.status_code,
            'content_type': content_type,
        }, retry_attempted=retry_attempted)

    error = RuntimeError(f'Too many redirects (>{MAX_REDIRECTS})')
    _annotate_and_raise(error, make_diagnostics(
        url,
        'http',
        'redirect',
        'policy_rejection',
        http_attempted=True,
        http_retry_attempted=retry_attempted,
    ))
