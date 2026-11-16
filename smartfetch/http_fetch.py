import os
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

from .diagnostics import (
    attach_diagnostics,
    diagnostics_for_exception,
    make_diagnostics,
)
from .security import (
    DNSResolutionError,
    install_dns_rebinding_protection,
    validate_public_url,
)

MAX_BYTES = int(os.getenv('MAX_RESPONSE_BYTES', '10485760'))
CONNECT_TIMEOUT = float(os.getenv('FETCH_CONNECT_TIMEOUT_SECONDS', '3.0'))
READ_TIMEOUT = float(os.getenv('FETCH_READ_TIMEOUT_SECONDS', '5.0'))
FETCH_TIMEOUT = float(os.getenv('FETCH_TIMEOUT_SECONDS', '10.0'))
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
MAX_REDIRECTS = 5
TRANSIENT_STATUS_CODES = {502, 503, 504}

SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({
    'User-Agent': 'SmartFetch/0.1 (+agent web retrieval prototype)',
    'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1',
    'Accept-Language': 'en-US,en;q=0.8',
})
# Mount SSRF and DNS rebinding protection adapter on retrieval session
install_dns_rebinding_protection(SESSION)


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


def http_fetch(url: str, *, _attempt_state=None) -> dict:
    started_at = time.perf_counter()
    try:
        current = validate_public_url(url)
    except Exception as error:
        is_dns = isinstance(error, DNSResolutionError)
        _annotate_and_raise(error, make_diagnostics(
            url,
            'http',
            'dns' if is_dns else 'validate',
            'dns' if is_dns else 'policy_rejection',
        ))

    retry_attempted = False
    seen_urls = {current}
    for _ in range(MAX_REDIRECTS + 1):
        if time.perf_counter() - started_at > FETCH_TIMEOUT:
            error = requests.Timeout(f'Fetch exceeded total timeout of {FETCH_TIMEOUT}s')
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                'connect',
                'timeout',
                http_attempted=True,
                http_retry_attempted=retry_attempted,
            ))

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
            status_code = response.status_code
            location = response.headers.get('Location')
            response.close()
            if not location:
                error = RuntimeError(
                    f'Redirect {status_code} missing Location header'
                )
                _annotate_and_raise(error, make_diagnostics(
                    url,
                    'http',
                    'redirect',
                    'invalid_content',
                    http_attempted=True,
                    http_retry_attempted=retry_attempted,
                    upstream_status=status_code,
                ))
            next_url = urljoin(current, location)
            if next_url in seen_urls:
                error = RuntimeError(f'Redirect loop detected: {next_url}')
                _annotate_and_raise(error, make_diagnostics(
                    url,
                    'http',
                    'redirect',
                    'policy_rejection',
                    http_attempted=True,
                    http_retry_attempted=retry_attempted,
                    upstream_status=status_code,
                ))
            try:
                current = validate_public_url(next_url)
            except Exception as error:
                is_dns = isinstance(error, DNSResolutionError)
                _annotate_and_raise(error, make_diagnostics(
                    url,
                    'http',
                    'dns' if is_dns else 'redirect',
                    'dns' if is_dns else 'policy_rejection',
                    http_attempted=True,
                    http_retry_attempted=retry_attempted,
                    upstream_status=status_code,
                ))
            seen_urls.add(current)
            continue

        if response.status_code >= 400:
            status_code = response.status_code
            response.close()
            failure_code = (
                'blocked_response'
                if status_code in {401, 403, 407, 429}
                else 'upstream_status'
            )
            error = RuntimeError(f'HTTP status {status_code}')
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                'response',
                failure_code,
                http_attempted=True,
                http_retry_attempted=retry_attempted,
                upstream_status=status_code,
            ))

        content_type = response.headers.get('Content-Type', '').lower()
        parsed_path = urlparse(current).path.lower()
        is_pdf_content_type = 'application/pdf' in content_type
        is_octet_pdf = 'application/octet-stream' in content_type and parsed_path.endswith('.pdf')
        is_pdf_candidate = is_pdf_content_type or is_octet_pdf

        if not (any(x in content_type for x in ('text/html', 'application/xhtml+xml', 'text/plain')) or is_pdf_candidate):
            response.close()
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
            response.close()
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
                    response.close()
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
            if diagnostics_for_exception(error) is not None:
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
        finally:
            response.close()

        raw = b''.join(chunks)

        if is_pdf_candidate:
            if not raw.startswith(b'%PDF-'):
                error = RuntimeError('Invalid PDF content: missing %PDF- header')
                _annotate_and_raise(error, make_diagnostics(
                    url,
                    'http',
                    'response',
                    'invalid_content',
                    http_attempted=True,
                    http_retry_attempted=retry_attempted,
                ))
            if isinstance(_attempt_state, dict):
                _attempt_state['retry_attempted'] = retry_attempted
            return {
                'raw_bytes': raw,
                'final_url': current,
                'status_code': response.status_code,
                'content_type': 'application/pdf',
                'is_pdf': True,
            }

        encoding = response.encoding or 'utf-8'
        try:
            html = raw.decode(encoding, errors='replace')
        except LookupError:
            html = raw.decode('utf-8', errors='replace')

        if isinstance(_attempt_state, dict):
            _attempt_state['retry_attempted'] = retry_attempted
        return {
            'html': html,
            'final_url': current,
            'status_code': response.status_code,
            'content_type': content_type,
        }

    error = RuntimeError(f'Too many redirects (>{MAX_REDIRECTS})')
    _annotate_and_raise(error, make_diagnostics(
        url,
        'http',
        'redirect',
        'policy_rejection',
        http_attempted=True,
        http_retry_attempted=retry_attempted,
    ))
