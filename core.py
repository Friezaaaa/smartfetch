import time

from .browser_fetch import browser_fetch
from .extract import extract_content
from .http_fetch import http_fetch
from .limits import normalize_max_chars, shape_output


def smart_fetch(url: str, force_browser: bool = False, max_chars=None) -> dict:
    started = time.perf_counter()
    max_chars = normalize_max_chars(max_chars)
    http_error = None

    if not force_browser:
        try:
            page = http_fetch(url)
            extracted = extract_content(page['html'], page['final_url'])
            if not extracted['low_quality']:
                return shape_output({
                    'success': True,
                    'requested_url': url,
                    'final_url': page['final_url'],
                    'status_code': page['status_code'],
                    'render_method': 'http',
                    'elapsed_ms': round((time.perf_counter() - started) * 1000),
                    **extracted,
                }, max_chars)
            http_error = f"HTTP extraction looked incomplete ({len(extracted['content'])} chars)"
        except Exception as exc:
            http_error = str(exc)

    try:
        page = browser_fetch(url)
        extracted = extract_content(page['html'], page['final_url'])
        if not extracted['content']:
            raise RuntimeError('Browser rendered page but no useful text was extracted')
        return shape_output({
            'success': True,
            'requested_url': url,
            'final_url': page['final_url'],
            'status_code': page['status_code'],
            'render_method': 'browser',
            'elapsed_ms': round((time.perf_counter() - started) * 1000),
            'fallback_reason': 'forced' if force_browser else http_error,
            **extracted,
        }, max_chars)
    except Exception as browser_error:
        raise RuntimeError(f'SmartFetch failed. HTTP: {http_error or "skipped"}. Browser: {browser_error}') from browser_error
