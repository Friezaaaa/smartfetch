import asyncio
import json
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .access_logging import uvicorn_log_config
from .activity import activity_context, emit_activity
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
from .diagnostics import (
    conservative_failure_activity_fields,
    failure_activity_fields,
)
from .discovery import (
    docs_html,
    llms_text,
    openapi_document,
    public_urls,
    robots_text,
    sitemap_xml,
    x402_manifest,
)
from .mcp_server import (
    MCP_PATH,
    MCP_TOOL,
    MCP_TOOLS,
    MCP_TRANSPORT,
    create_smartfetch_mcp,
)
from .payments import (
    BASE_MAINNET,
    X402Settings,
    install_x402,
    load_x402_settings,
)


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


def _client_category(request: Request) -> str:
    user_agent = request.headers.get('user-agent', '').lower()
    if 'mcp' in user_agent:
        return 'mcp-client'
    if any(marker in user_agent for marker in ('mozilla/', 'chrome/', 'safari/')):
        return 'browser'
    if any(marker in user_agent for marker in ('python', 'httpx', 'requests')):
        return 'python-http'
    if any(marker in user_agent for marker in ('axios', 'node', 'typescript')):
        return 'javascript-http'
    return 'other'


def _http_payment_present(request: Request) -> bool:
    return any(
        name in request.headers
        for name in ('payment-signature', 'x-payment')
    )


def _payment_activity_fields(settings, present: bool, stage: str):
    return {
        'payment_present': present,
        'payment_stage': stage,
        'payment_network': settings.network,
        'payment_asset': 'USDC',
        'payment_amount': settings.price,
    }


def _not_found(request: Request) -> JSONResponse:
    return _json_response(request, 404, {
        'success': False,
        'error_code': 'not_found',
        'error': 'Not found',
    })


async def _mcp_activity_operation(request: Request):
    if request.method != 'POST' or request.url.path != MCP_PATH:
        return None, None, False
    try:
        length = int(request.headers.get('content-length') or 0)
    except ValueError:
        return None, None, False
    if length <= 0 or length > MAX_REQUEST_BODY_BYTES:
        return None, None, False
    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None, False
    if not isinstance(payload, dict):
        return None, None, False
    method = payload.get('method')
    if not isinstance(method, str):
        return None, None, False
    tool = None
    payment_present = False
    if method == 'tools/call':
        params = payload.get('params')
        if isinstance(params, dict):
            if params.get('name') in MCP_TOOLS:
                tool = params['name']
            metadata = params.get('_meta')
            payment_present = (
                isinstance(metadata, dict)
                and 'x402/payment' in metadata
            )
    return method, tool, payment_present


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


async def _run_mcp_fetch(
    url: str,
    force_browser: bool,
    max_chars: int,
) -> dict:
    if not isinstance(url, str) or not url.strip():
        raise ValueError('url must be a non-empty string')
    if not _FETCH_SLOTS.acquire(blocking=False):
        raise RuntimeError('SmartFetch is at capacity; retry shortly')
    try:
        try:
            result = await _run_fetch(url.strip(), force_browser, max_chars)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                'Fetch exceeded the service time limit'
            ) from exc
        output = dict(result)
        output['request_id'] = uuid.uuid4().hex[:16]
        output['service_version'] = SERVICE_VERSION
        return output
    finally:
        _FETCH_SLOTS.release()


def create_app(
    payment_settings: Optional[X402Settings] = None,
) -> FastAPI:
    settings = payment_settings or load_x402_settings()
    payment_mode = (
        'not-enabled-yet'
        if not settings.enabled
        else (
            'x402-enabled-mainnet'
            if settings.network == BASE_MAINNET
            else 'x402-enabled-testnet'
        )
    )
    smartfetch_mcp = create_smartfetch_mcp(settings, _run_mcp_fetch)
    application = FastAPI(
        title=SERVICE_NAME,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=smartfetch_mcp.lifespan,
    )
    application.state.smartfetch_mcp = smartfetch_mcp

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

    @application.get('/.well-known/glama.json')
    async def glama_ownership():
        return JSONResponse({
            '$schema': 'https://glama.ai/mcp/schemas/connector.json',
            'maintainers': [
                {
                    'email': 'smartfetch.contact@gmail.com',
                },
            ],
        })

    async def metadata(request: Request):
        urls = public_urls(request)
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
            'payment': payment_mode,
            'mcp': {
                'enabled': True,
                'path': MCP_PATH,
                'transport': MCP_TRANSPORT,
                'tool': MCP_TOOL,
                'tools': list(MCP_TOOLS),
                'url': urls['mcp'],
            },
            'discovery': {
                'x402': urls['x402'],
                'docs': urls['docs'],
                'openapi': urls['openapi'],
                'llms': urls['llms'],
                'robots': urls['robots'],
                'sitemap': urls['sitemap'],
            },
        })

    application.add_api_route('/', metadata, methods=['GET'])
    application.add_api_route('/meta', metadata, methods=['GET'])

    @application.get('/.well-known/x402')
    async def x402_discovery(request: Request):
        return JSONResponse(x402_manifest(public_urls(request), settings))

    @application.get('/docs', response_class=HTMLResponse)
    async def discovery_docs(request: Request):
        return HTMLResponse(docs_html(public_urls(request)))

    @application.get('/openapi.json')
    async def discovery_openapi(request: Request):
        payment_requirement = (
            smartfetch_mcp.accepts[0]
            if smartfetch_mcp.accepts
            else None
        )
        return JSONResponse(openapi_document(
            public_urls(request),
            settings,
            payment_requirement,
        ))

    @application.get('/llms.txt', response_class=PlainTextResponse)
    async def discovery_llms(request: Request):
        return PlainTextResponse(llms_text(public_urls(request)))

    @application.get('/robots.txt', response_class=PlainTextResponse)
    async def discovery_robots(request: Request):
        return PlainTextResponse(robots_text(public_urls(request)))

    @application.get('/sitemap.xml')
    async def discovery_sitemap(request: Request):
        return Response(
            content=sitemap_xml(public_urls(request)),
            media_type='application/xml',
        )

    @application.post('/fetch')
    async def fetch(request: Request):
        if getattr(request.state, 'payment_payload', None) is not None:
            emit_activity(
                'payment_verified',
                transport='http',
                tool=MCP_TOOL,
                stage='verification',
                outcome='verified',
                **_payment_activity_fields(
                    settings,
                    True,
                    'verification',
                ),
            )
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
            fetch_started = time.perf_counter()
            emit_activity(
                'tool_started',
                transport='http',
                tool=MCP_TOOL,
                stage='execution',
                outcome='started',
            )
            try:
                result = await _run_fetch(url.strip(), force_browser, max_chars)
            except asyncio.TimeoutError:
                emit_activity(
                    'tool_failed',
                    transport='http',
                    tool=MCP_TOOL,
                    stage='execution',
                    outcome='timeout',
                    failure_reason='timeout',
                    status=504,
                    duration_ms=(time.perf_counter() - fetch_started) * 1000,
                    **conservative_failure_activity_fields(
                        url.strip(),
                        'timeout',
                    ),
                )
                return _json_response(request, 504, {
                    'success': False,
                    'error_code': 'fetch_timeout',
                    'error': 'Fetch exceeded the service time limit',
                })
            except ValueError as exc:
                emit_activity(
                    'tool_failed',
                    transport='http',
                    tool=MCP_TOOL,
                    stage='execution',
                    outcome='rejected',
                    failure_reason='target_rejected',
                    status=400,
                    duration_ms=(time.perf_counter() - fetch_started) * 1000,
                    **conservative_failure_activity_fields(
                        url.strip(),
                        'policy_rejection',
                        'validate',
                    ),
                )
                return _json_response(request, 400, {
                    'success': False,
                    'error_code': 'invalid_or_blocked_target',
                    'error': str(exc),
                })
            except Exception as exc:
                emit_activity(
                    'tool_failed',
                    transport='http',
                    tool=MCP_TOOL,
                    stage='execution',
                    outcome='failed',
                    failure_reason='retrieval_failed',
                    status=502,
                    duration_ms=(time.perf_counter() - fetch_started) * 1000,
                    **failure_activity_fields(
                        exc,
                        url.strip(),
                    ),
                )
                return _json_response(request, 502, {
                    'success': False,
                    'error_code': 'fetch_failed',
                    'error': str(exc),
                })
            result['request_id'] = _request_id(request)
            result['service_version'] = SERVICE_VERSION
            emit_activity(
                'tool_completed',
                transport='http',
                tool=MCP_TOOL,
                stage='execution',
                outcome='completed',
                status=200,
                duration_ms=(time.perf_counter() - fetch_started) * 1000,
            )
            return _json_response(request, 200, result)
        finally:
            _FETCH_SLOTS.release()

    application.router.routes.append(smartfetch_mcp.route)

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
        method, tool, mcp_payment_present = await _mcp_activity_operation(
            request
        )
        is_http_fetch = (
            request.method == 'POST' and request.url.path == '/fetch'
        )
        with activity_context(
            request_id,
            route=(MCP_PATH if method is not None else request.url.path),
            client_category=_client_category(request),
        ):
            if is_http_fetch:
                emit_activity(
                    'tool_call_attempted',
                    transport='http',
                    tool=MCP_TOOL,
                    stage='request',
                    outcome='received',
                    payment_present=_http_payment_present(request),
                )
            if method == 'tools/call':
                emit_activity(
                    'tool_call_attempted',
                    transport='mcp',
                    tool=tool,
                    stage='request',
                    outcome='received',
                    payment_present=mcp_payment_present,
                )
            response = await call_next(request)
            if method == 'initialize':
                emit_activity(
                    'mcp_initialized',
                    transport='mcp',
                    stage='response',
                    outcome=(
                        'completed' if response.status_code < 400 else 'failed'
                    ),
                    status=response.status_code,
                )
            elif method == 'tools/list':
                emit_activity(
                    'tools_listed',
                    transport='mcp',
                    stage='response',
                    outcome=(
                        'completed' if response.status_code < 400 else 'failed'
                    ),
                    status=response.status_code,
                )
            if is_http_fetch:
                if (
                    response.status_code == 402
                    and getattr(request.state, 'payment_payload', None) is None
                ):
                    emit_activity(
                        'payment_challenged',
                        transport='http',
                        tool=MCP_TOOL,
                        stage='challenge',
                        outcome='payment_required',
                        status=402,
                        failure_reason=(
                            'payment_rejected'
                            if _http_payment_present(request)
                            else 'payment_required'
                        ),
                        **_payment_activity_fields(
                            settings,
                            _http_payment_present(request),
                            'challenge',
                        ),
                    )
                elif (
                    response.status_code < 400
                    and 'payment-response' in response.headers
                ):
                    emit_activity(
                        'payment_settled',
                        transport='http',
                        tool=MCP_TOOL,
                        stage='settlement',
                        outcome='settled',
                        status=response.status_code,
                        **_payment_activity_fields(
                            settings,
                            True,
                            'settlement',
                        ),
                    )
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


def create_uvicorn_config(application=None) -> uvicorn.Config:
    forwarded_allow_ips = os.environ.get('FORWARDED_ALLOW_IPS')
    if forwarded_allow_ips is None and os.environ.get('RAILWAY_ENVIRONMENT_ID'):
        forwarded_allow_ips = '*'
    return uvicorn.Config(
        app if application is None else application,
        host=HOST,
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        log_config=uvicorn_log_config(),
    )


def main():
    print(
        f'{SERVICE_NAME} {SERVICE_VERSION} listening on http://{HOST}:{PORT}',
        flush=True,
    )
    uvicorn.Server(create_uvicorn_config()).run()


if __name__ == '__main__':
    main()
