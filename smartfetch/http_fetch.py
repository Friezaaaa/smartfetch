import os
import time
from urllib.parse import urljoin

import requests

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
        return response

    raise RuntimeError('Transient request retry exhausted')


def http_fetch(url: str) -> dict:
    current = validate_public_url(url)

    for _ in range(MAX_REDIRECTS + 1):
        response = _request_with_retry(current)

        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get('Location')
            if not location:
                raise RuntimeError(f'Redirect {response.status_code} missing Location header')
            current = validate_public_url(urljoin(current, location))
            continue

        if response.status_code >= 400:
            raise RuntimeError(f'HTTP status {response.status_code}')

        content_type = response.headers.get('Content-Type', '').lower()
        if not any(x in content_type for x in ('text/html', 'application/xhtml+xml', 'text/plain')):
            raise RuntimeError(f'Unsupported content-type: {content_type or "unknown"}')

        declared = int(response.headers.get('Content-Length') or 0)
        if declared > MAX_BYTES:
            raise RuntimeError(f'Response too large ({declared} bytes)')

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_BYTES:
                raise RuntimeError(f'Response exceeded {MAX_BYTES} byte limit')
            chunks.append(chunk)

        raw = b''.join(chunks)
        encoding = response.encoding or 'utf-8'
        try:
            html = raw.decode(encoding, errors='replace')
        except LookupError:
            html = raw.decode('utf-8', errors='replace')

        return {
            'html': html,
            'final_url': current,
            'status_code': response.status_code,
            'content_type': content_type,
        }

    raise RuntimeError(f'Too many redirects (>{MAX_REDIRECTS})')
