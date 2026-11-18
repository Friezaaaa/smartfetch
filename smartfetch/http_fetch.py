import ipaddress
import os
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

try:
    from curl_cffi import CurlOpt
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional fallback if curl_cffi unavailable
    CurlOpt = None
    curl_requests = None

from .diagnostics import (
    attach_diagnostics,
    diagnostics_for_exception,
    make_diagnostics,
)
from .security import (
    DNSResolutionError,
    install_dns_rebinding_protection,
    resolve_and_pin_public_url,
    validate_public_url,
)

MAX_BYTES = int(os.getenv('MAX_RESPONSE_BYTES', '10485760'))
CONNECT_TIMEOUT = float(os.getenv('FETCH_CONNECT_TIMEOUT_SECONDS', '3.0'))
READ_TIMEOUT = float(os.getenv('FETCH_READ_TIMEOUT_SECONDS', '5.0'))
FETCH_TIMEOUT = float(os.getenv('FETCH_TIMEOUT_SECONDS', '10.0'))
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
MAX_REDIRECTS = 5
TRANSIENT_STATUS_CODES = {502, 503, 504}

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/131.0.0.0 Safari/537.36'
)

SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({
    'User-Agent': DEFAULT_USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Upgrade-Insecure-Requests': '1',
})
# Mount SSRF and DNS rebinding protection adapter on retrieval session
install_dns_rebinding_protection(SESSION)


def _format_curl_resolve_entry(hostname: str, port: int, addresses: tuple[str, ...]) -> list[str]:
    """Builds a libcurl CURLOPT_RESOLVE list pinning validated public IP addresses."""
    ip_addrs: list[str] = []
    for addr in addresses:
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError:
            continue
        ip_addrs.append(f'[{ip_obj.compressed}]' if ip_obj.version == 6 else ip_obj.compressed)
    if not ip_addrs:
        return []
    return [f'{hostname}:{port}:{",".join(ip_addrs)}']


def _curl_cffi_get(url: str):
    """Performs a Chrome TLS-impersonating GET with strict SSRF IP-pinning."""
    pinned = resolve_and_pin_public_url(url)
    curl_opts = {}
    if CurlOpt is not None:
        resolve_entries = _format_curl_resolve_entry(pinned.hostname, pinned.port, pinned.addresses)
        if resolve_entries:
            curl_opts[CurlOpt.RESOLVE] = resolve_entries

    return curl_requests.get(
        pinned.url,
        impersonate='chrome',
        headers={
            'Accept': SESSION.headers.get('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
            'Accept-Language': SESSION.headers.get('Accept-Language', 'en-US,en;q=0.9'),
            'Upgrade-Insecure-Requests': '1',
        },
        allow_redirects=False,
        proxies={},
        timeout=TIMEOUT,
        stream=True,
        curl_options=curl_opts or None,
    )


def _should_use_curl_cffi() -> bool:
    """Use curl_cffi only when SESSION.get and SESSION.send are not mocked in unit tests."""
    if curl_requests is None:
        return False
    return (
        getattr(SESSION.get, '__func__', None) is requests.Session.get
        and getattr(SESSION.send, '__func__', None) is requests.Session.send
    )


def _request_with_retry(url: str):
    use_curl = _should_use_curl_cffi()
    transient_excs: tuple[type[BaseException], ...] = (requests.ConnectionError, requests.Timeout)
    if use_curl:
        transient_excs = transient_excs + (
            curl_requests.exceptions.ConnectionError,
            curl_requests.exceptions.Timeout,
        )

    for attempt in range(2):
        try:
            if use_curl:
                response = _curl_cffi_get(url)
            else:
                response = SESSION.get(
                    url,
                    allow_redirects=False,
                    timeout=TIMEOUT,
                    stream=True,
                )
        except transient_excs:
            if attempt == 0:
                continue
            raise

        if response.status_code in TRANSIENT_STATUS_CODES and attempt == 0:
            response.close()
            continue
        return response, attempt > 0

    raise RuntimeError('Transient request retry exhausted')


def _network_failure(error: BaseException) -> tuple[str, str]:
    ssl_types: tuple[type[BaseException], ...] = (requests.exceptions.SSLError,)
    timeout_types: tuple[type[BaseException], ...] = (requests.Timeout,)
    if curl_requests is not None:
        ssl_types = ssl_types + (curl_requests.exceptions.SSLError,)
        timeout_types = timeout_types + (curl_requests.exceptions.Timeout,)

    if isinstance(error, ssl_types):
        return 'tls', 'tls'
    if isinstance(error, timeout_types):
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
        except (DNSResolutionError, ValueError) as error:
            is_dns = isinstance(error, DNSResolutionError)
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                'dns' if is_dns else 'validate',
                'dns' if is_dns else 'policy_rejection',
                http_attempted=True,
                http_retry_attempted=retry_attempted,
            ))
        except Exception as error:
            phase, failure_code = _network_failure(error)
            retry_types: tuple[type[BaseException], ...] = (requests.ConnectionError, requests.Timeout)
            if curl_requests is not None:
                retry_types = retry_types + (
                    curl_requests.exceptions.ConnectionError,
                    curl_requests.exceptions.Timeout,
                )
            _annotate_and_raise(error, make_diagnostics(
                url,
                'http',
                phase,
                failure_code,
                http_attempted=True,
                http_retry_attempted=isinstance(error, retry_types),
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

        SUPPORTED_TEXT_TYPES = (
            'text/html',
            'application/xhtml+xml',
            'text/plain',
            'text/markdown',
            'text/x-markdown',
            'text/csv',
            'application/json',
            'application/xml',
            'text/xml',
            'application/rss+xml',
            'application/atom+xml',
        )
        if not (any(x in content_type for x in SUPPORTED_TEXT_TYPES) or is_pdf_candidate):
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
        safe_headers = {
            k.lower(): v for k, v in response.headers.items()
            if k.lower() != 'set-cookie'
        }

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
                'headers': safe_headers,
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
            'headers': safe_headers,
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
