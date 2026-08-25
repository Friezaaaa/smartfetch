import json
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .config import (
    HOST, MAX_CONCURRENT_FETCHES, MAX_REQUEST_BODY_BYTES, PORT,
    RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE, SERVICE_NAME, SERVICE_VERSION,
    TOTAL_REQUEST_TIMEOUT_SECONDS,
)
from .core import smart_fetch

_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, MAX_CONCURRENT_FETCHES), thread_name_prefix='fetch')
_FETCH_SLOTS = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_FETCHES))
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS = defaultdict(deque)
_STARTED = time.time()


def _rate_allowed(client: str) -> bool:
    now = time.time()
    cutoff = now - 60
    with _RATE_LOCK:
        q = _RATE_BUCKETS[client]
        while q and q[0] < cutoff:
            q.popleft()
        limit = RATE_LIMIT_PER_MINUTE + RATE_LIMIT_BURST
        if len(q) >= limit:
            return False
        q.append(now)
        # Opportunistic cleanup to stop the dict growing forever.
        if len(_RATE_BUCKETS) > 10000:
            stale = [k for k, v in _RATE_BUCKETS.items() if not v or v[-1] < cutoff]
            for k in stale[:2000]:
                _RATE_BUCKETS.pop(k, None)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = f'SmartFetch/{SERVICE_VERSION}'

    def _request_id(self):
        return getattr(self, 'request_id', None) or uuid.uuid4().hex[:16]

    def _json(self, status: int, payload: dict, headers=None):
        payload = dict(payload)
        payload.setdefault('request_id', self._request_id())
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Request-ID', self._request_id())
        self.send_header('Content-Length', str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _client_key(self):
        # Do not trust X-Forwarded-For by default. A reverse proxy can be configured
        # later to overwrite it safely if desired.
        return self.client_address[0] if self.client_address else 'unknown'

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/health':
            return self._json(200, {
                'ok': True,
                'service': SERVICE_NAME,
                'version': SERVICE_VERSION,
                'uptime_seconds': int(time.time() - _STARTED),
            })
        if path in {'/', '/meta'}:
            return self._json(200, {
                'service': SERVICE_NAME,
                'version': SERVICE_VERSION,
                'description': 'Reliable public-web retrieval for AI agents: URL in, clean text/Markdown/links out.',
                'endpoint': {'method': 'POST', 'path': '/fetch'},
                'input': {'url': 'https://…', 'max_chars': 20000, 'force_browser': False},
                'payment': 'not-enabled-yet',
            })
        return self._json(404, {'success': False, 'error_code': 'not_found', 'error': 'Not found'})

    def do_POST(self):
        self.request_id = uuid.uuid4().hex[:16]
        path = urlparse(self.path).path
        if path != '/fetch':
            return self._json(404, {'success': False, 'error_code': 'not_found', 'error': 'Not found'})

        if not _rate_allowed(self._client_key()):
            return self._json(429, {
                'success': False,
                'error_code': 'rate_limited',
                'error': 'Rate limit exceeded; retry shortly',
            }, {'Retry-After': '60'})

        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BODY_BYTES:
            return self._json(400, {
                'success': False,
                'error_code': 'invalid_body_size',
                'error': 'Invalid request body size',
            })

        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return self._json(400, {'success': False, 'error_code': 'invalid_json', 'error': 'Invalid JSON'})
        if not isinstance(body, dict):
            return self._json(400, {'success': False, 'error_code': 'invalid_request', 'error': 'JSON body must be an object'})

        url = body.get('url')
        if not isinstance(url, str) or not url.strip():
            return self._json(400, {
                'success': False,
                'error_code': 'invalid_request',
                'error': 'Body must include a non-empty string field: url',
            })
        force_browser = body.get('force_browser') is True
        max_chars = body.get('max_chars')

        if not _FETCH_SLOTS.acquire(blocking=False):
            return self._json(503, {
                'success': False,
                'error_code': 'busy',
                'error': 'SmartFetch is at capacity; retry shortly',
            }, {'Retry-After': '2'})
        try:
            future = _EXECUTOR.submit(smart_fetch, url.strip(), force_browser, max_chars)
            try:
                result = future.result(timeout=TOTAL_REQUEST_TIMEOUT_SECONDS)
            except FutureTimeout:
                future.cancel()
                return self._json(504, {
                    'success': False,
                    'error_code': 'fetch_timeout',
                    'error': 'Fetch exceeded the service time limit',
                })
            except ValueError as exc:
                return self._json(400, {
                    'success': False,
                    'error_code': 'invalid_or_blocked_target',
                    'error': str(exc),
                })
            except Exception as exc:
                return self._json(502, {
                    'success': False,
                    'error_code': 'fetch_failed',
                    'error': str(exc),
                })
            result['request_id'] = self._request_id()
            result['service_version'] = SERVICE_VERSION
            return self._json(200, result)
        finally:
            _FETCH_SLOTS.release()

    def log_message(self, fmt, *args):
        print(json.dumps({
            'ts': round(time.time(), 3),
            'request_id': self._request_id(),
            'client': self._client_key(),
            'message': fmt % args,
        }, separators=(',', ':')), flush=True)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f'{SERVICE_NAME} {SERVICE_VERSION} listening on http://{HOST}:{PORT}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _EXECUTOR.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
    main()
