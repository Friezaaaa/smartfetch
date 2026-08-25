import asyncio
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import (
    HOST,
    MAX_CONCURRENT_FETCHES,
    MAX_REQUEST_BODY_BYTES,
    PORT,
    RATE_LIMIT_BURST,
    RATE_LIMIT_PER_MINUTE,
    SERVICE_NAME,
    SERVICE_VERSION,
    TOTAL_REQUEST_TIMEOUT_SECONDS,
)
from .core import smart_fetch
from .payments import X402Settings, install_x402, load_x402_settings


_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, MAX_CONCURRENT_FETCHES),
    thread_name_prefix='fetch',
)
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
        if len(_RATE_BUCKETS) > 10000:
            stale = [
                key for key, values in _RATE_BUCKETS.items()
                if not values or values[-1] < cutoff
            ]
            for key in stale[:2000]:
                _RATE_BUCKETS.pop(key, None)
        return True


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, 'request_id', None)
    if request_id is None:
        request_id = uuid.uuid4().hex[:16]
        request.state.request_id = request_id
    return request_id


def _json_response(
    request: Request,
    status: int,
    payload: dict,
    headers: Optional[dict] = None,
) -> JSONResponse:
    body = dict(payload)
    body.setdefault('request_id', _request_id(request))
    response_headers = {'Content-Type': 'application/json; charset=utf-8'}
    if headers:
        response_headers.update({key: str(value) for key, value in headers.items()})
    return JSONResponse(
        status_code=status,
        content=body,
        headers=response_headers,
    )


def _client_key(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def _not_found(request: Request) -> JSONResponse:
    return _json_response(request, 404, {
        'success': False,
        'error_code': 'not_found',
        'error': 'Not found',
    })


async def _run_fetch(url: str, force_browser: bool, max_chars):
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        _EXECUTOR,
        smart_fetch,
        url,
        force_browser,
        max_chars,
    )
    try:
        return await asyncio.wait_for(
            future,
            timeout=TOTAL_REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        future.cancel()
        raise


def create_app(
    payment_settings: Optional[X402Settings] = None,
) -> FastAPI:
    settings = payment_settings or load_x402_settings()
    application = FastAPI(
        title=SERVICE_NAME,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def framework_not_found(request: Request, _exception):
        return _not_found(request)

    application.add_exception_handler(404, framework_not_found)
    application.add_exception_handler(405, framework_not_found)

    @application.get('/health')
    async def health(request: Request):
        return _json_response(request, 200, {
            'ok': True,
            'service': SERVICE_NAME,
            'version': SERVICE_VERSION,
            'uptime_seconds': int(time.time() - _STARTED),
        })

    async def metadata(request: Request):
        return _json_response(request, 200, {
            'service': SERVICE_NAME,
            'version': SERVICE_VERSION,
            'description': (
                'Reliable public-web retrieval for AI agents: URL in, '
                'clean text/Markdown/links out.'
            ),
            'endpoint': {'method': 'POST', 'path': '/fetch'},
            'input': {
                'url': 'https://…',
                'max_chars': 20000,
                'force_browser': False,
            },
            'payment': (
                'x402-enabled-testnet'
                if settings.enabled
                else 'not-enabled-yet'
            ),
        })

    application.add_api_route('/', metadata, methods=['GET'])
    application.add_api_route('/meta', metadata, methods=['GET'])

    @application.post('/fetch')
    async def fetch(request: Request):
        if not _rate_allowed(_client_key(request)):
            return _json_response(request, 429, {
                'success': False,
                'error_code': 'rate_limited',
                'error': 'Rate limit exceeded; retry shortly',
            }, {'Retry-After': '60'})

        try:
            length = int(request.headers.get('content-length') or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BODY_BYTES:
            return _json_response(request, 400, {
                'success': False,
                'error_code': 'invalid_body_size',
                'error': 'Invalid request body size',
            })

        raw_body = await request.body()
        if len(raw_body) > MAX_REQUEST_BODY_BYTES:
            return _json_response(request, 400, {
                'success': False,
                'error_code': 'invalid_body_size',
                'error': 'Invalid request body size',
            })
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json_response(request, 400, {
                'success': False,
                'error_code': 'invalid_json',
                'error': 'Invalid JSON',
            })
        if not isinstance(body, dict):
            return _json_response(request, 400, {
                'success': False,
                'error_code': 'invalid_request',
                'error': 'JSON body must be an object',
            })

        url = body.get('url')
        if not isinstance(url, str) or not url.strip():
            return _json_response(request, 400, {
                'success': False,
                'error_code': 'invalid_request',
                'error': 'Body must include a non-empty string field: url',
            })
        force_browser = body.get('force_browser') is True
        max_chars = body.get('max_chars')

        if not _FETCH_SLOTS.acquire(blocking=False):
            return _json_response(request, 503, {
                'success': False,
                'error_code': 'busy',
                'error': 'SmartFetch is at capacity; retry shortly',
            }, {'Retry-After': '2'})
        try:
            try:
                result = await _run_fetch(url.strip(), force_browser, max_chars)
            except asyncio.TimeoutError:
                return _json_response(request, 504, {
                    'success': False,
                    'error_code': 'fetch_timeout',
                    'error': 'Fetch exceeded the service time limit',
                })
            except ValueError as exc:
                return _json_response(request, 400, {
                    'success': False,
                    'error_code': 'invalid_or_blocked_target',
                    'error': str(exc),
                })
            except Exception as exc:
                return _json_response(request, 502, {
                    'success': False,
                    'error_code': 'fetch_failed',
                    'error': str(exc),
                })
            result['request_id'] = _request_id(request)
            result['service_version'] = SERVICE_VERSION
            return _json_response(request, 200, result)
        finally:
            _FETCH_SLOTS.release()

    @application.api_route('/{path:path}', methods=[
        'GET',
        'POST',
        'PUT',
        'PATCH',
        'DELETE',
        'HEAD',
        'OPTIONS',
    ])
    async def not_found(request: Request, path: str):
        return _not_found(request)

    install_x402(application, settings)

    @application.middleware('http')
    async def response_headers(request: Request, call_next):
        request_id = _request_id(request)
        response = await call_next(request)
        if (
            response.status_code == 402
            and 'application/json' in response.headers.get('content-type', '')
        ):
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(
                    chunk.encode('utf-8') if isinstance(chunk, str) else chunk
                )
            raw_body = b''.join(chunks)
            headers = dict(response.headers)
            headers.pop('content-length', None)
            try:
                body = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = Response(
                    content=raw_body,
                    status_code=response.status_code,
                    headers=headers,
                    background=response.background,
                )
            else:
                if isinstance(body, dict):
                    body.setdefault('request_id', request_id)
                response = JSONResponse(
                    content=body,
                    status_code=response.status_code,
                    headers=headers,
                    background=response.background,
                )
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Request-ID'] = request_id
        return response

    return application


app = create_app()


def main():
    print(
        f'{SERVICE_NAME} {SERVICE_VERSION} listening on http://{HOST}:{PORT}',
        flush=True,
    )
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == '__main__':
    main()
