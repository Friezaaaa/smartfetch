import time

from .browser_fetch import browser_fetch
from .diagnostics import (
    RetrievalFailure,
    aggregate_browser_failure,
    attach_diagnostics,
    diagnostics_for_exception,
    make_diagnostics,
)
from .extract import extract_content
from .http_fetch import http_fetch
from .limits import normalize_max_chars, shape_output


def smart_fetch(url: str, force_browser: bool = False, max_chars=None) -> dict:
    started = time.perf_counter()
    max_chars = normalize_max_chars(max_chars)
    http_error = None
    http_diagnostics = None

    if not force_browser:
        try:
            page = http_fetch(url)
            try:
                extracted = extract_content(page['html'], page['final_url'])
            except Exception as error:
                annotated = attach_diagnostics(error, make_diagnostics(
                    url,
                    'http',
                    'extract',
                    'invalid_content',
                    http_attempted=True,
                    http_retry_attempted=bool(
                        getattr(page, 'retry_attempted', False)
                    ),
                ))
                if annotated is error:
                    raise
                raise annotated from error
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
            http_diagnostics = make_diagnostics(
                url,
                'http',
                'extract',
                'invalid_content',
                http_attempted=True,
                http_retry_attempted=bool(
                    getattr(page, 'retry_attempted', False)
                ),
            )
        except Exception as exc:
            http_error = str(exc)
            http_diagnostics = diagnostics_for_exception(exc) or make_diagnostics(
                url,
                'http',
                'response',
                'unknown',
                http_attempted=True,
            )

    try:
        page = browser_fetch(url)
        try:
            extracted = extract_content(page['html'], page['final_url'])
            if not extracted['content']:
                error = RuntimeError(
                    'Browser rendered page but no useful text was extracted'
                )
                annotated = attach_diagnostics(error, make_diagnostics(
                    url,
                    'browser',
                    'browser_extract',
                    'invalid_content',
                    browser_attempted=True,
                ))
                raise annotated
        except Exception as error:
            if diagnostics_for_exception(error) is not None:
                raise
            annotated = attach_diagnostics(error, make_diagnostics(
                url,
                'browser',
                'browser_extract',
                'invalid_content',
                browser_attempted=True,
            ))
            if annotated is error:
                raise
            raise annotated from error
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
        diagnostics = aggregate_browser_failure(
            url,
            browser_error,
            http_diagnostics,
            force_browser=force_browser,
        )
        raise RetrievalFailure(
            f'SmartFetch failed. HTTP: {http_error or "skipped"}. '
            f'Browser: {browser_error}',
            diagnostics,
        ) from browser_error
