import re
import time

from .browser_fetch import browser_fetch
from .cache import build_cache_key, get_cache_provider
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
from .observability import ObservabilityRecord, record_retrieval

SPA_ROOT_PATTERNS = [
    re.compile(r'<div[^>]+id=["\'](?:root|app|__next|___gatsby)["\'][^>]*>\s*</div>', re.IGNORECASE),
    re.compile(r'<app-root[^>]*>\s*</app-root>', re.IGNORECASE),
]

NOSCRIPT_JS_PATTERN = re.compile(
    r'<noscript[^>]*>.*?(?:enable javascript|requires javascript|javascript to run|turn on javascript).*?</noscript>',
    re.IGNORECASE | re.DOTALL,
)


def evaluate_escalation_heuristics(html: str, extracted_content: str, low_quality: bool) -> tuple[bool, str]:
    """Evaluates whether static HTML extraction is insufficient and requires browser escalation."""
    content_stripped = (extracted_content or "").strip()
    if low_quality or len(content_stripped) < 200:
        for pattern in SPA_ROOT_PATTERNS:
            if pattern.search(html):
                return True, "spa_root_tag"
        if NOSCRIPT_JS_PATTERN.search(html):
            return True, "noscript_warning"
        return True, "minimal_dom"

    for pattern in SPA_ROOT_PATTERNS:
        if pattern.search(html):
            return True, "spa_root_tag"

    if NOSCRIPT_JS_PATTERN.search(html) and len(content_stripped) < 500:
        return True, "noscript_warning"

    return False, "none"


def smart_fetch(
    url: str,
    force_browser: bool = False,
    max_chars=None,
    *,
    format: str = "markdown",
    render_mode: str = "auto",
    no_cache: bool = False,
) -> dict:
    started = time.perf_counter()
    max_chars = normalize_max_chars(max_chars)
    http_error = None
    http_diagnostics = None
    http_attempt_state = {}

    # Resolve render mode with backward compatibility for force_browser
    mode_lower = (render_mode or "auto").lower()
    if force_browser or mode_lower == "browser":
        resolved_render_mode = "browser"
    elif mode_lower in ("http_only", "http"):
        resolved_render_mode = "http_only"
    else:
        resolved_render_mode = "auto"

    cache_key = build_cache_key(url, format=format, render_mode=resolved_render_mode)

    # 1. Cache lookup
    if not no_cache:
        cached = get_cache_provider().get(cache_key)
        if cached is not None and cached.get("success"):
            obs = ObservabilityRecord(
                retrieval_tier="cache",
                cache_hit=True,
                http_latency_ms=0.0,
                browser_latency_ms=0.0,
                escalation_reason="none",
            )
            record_retrieval(obs)
            cached_result = dict(cached)
            cached_result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            cached_result["retrieval_tier"] = "cache"
            cached_result["cache_hit"] = True
            cached_result["telemetry"] = obs.to_dict()
            return shape_output(cached_result, max_chars)

    obs = ObservabilityRecord(
        retrieval_tier="http",
        cache_hit=False,
    )

    # 2. Tier 1: Fast HTTP (skipped if resolved_render_mode == "browser")
    if resolved_render_mode != "browser":
        http_start = time.perf_counter()
        try:
            page = http_fetch(url, _attempt_state=http_attempt_state)
            obs.http_latency_ms = round((time.perf_counter() - http_start) * 1000, 2)
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
                        http_attempt_state.get('retry_attempted', False)
                    ),
                ))
                if annotated is error:
                    raise
                raise annotated from error

            # Tier 2: Escalation Heuristics evaluation
            if resolved_render_mode == "http_only":
                obs.retrieval_tier = "http"
                record_retrieval(obs)
                result = shape_output({
                    'success': True,
                    'requested_url': url,
                    'final_url': page['final_url'],
                    'status_code': page['status_code'],
                    'render_method': 'http',
                    'retrieval_tier': 'http',
                    'cache_hit': False,
                    'elapsed_ms': round((time.perf_counter() - started) * 1000),
                    'telemetry': obs.to_dict(),
                    **extracted,
                }, max_chars)
                if not no_cache and page.get('status_code') == 200:
                    get_cache_provider().set(cache_key, result)
                return result

            should_escalate, escalation_reason = evaluate_escalation_heuristics(
                page['html'],
                extracted.get('content', ''),
                extracted.get('low_quality', False),
            )

            if not extracted['low_quality'] and not should_escalate:
                obs.retrieval_tier = "http"
                record_retrieval(obs)
                result = shape_output({
                    'success': True,
                    'requested_url': url,
                    'final_url': page['final_url'],
                    'status_code': page['status_code'],
                    'render_method': 'http',
                    'retrieval_tier': 'http',
                    'cache_hit': False,
                    'elapsed_ms': round((time.perf_counter() - started) * 1000),
                    'telemetry': obs.to_dict(),
                    **extracted,
                }, max_chars)
                if not no_cache and page.get('status_code') == 200:
                    get_cache_provider().set(cache_key, result)
                return result

            # Escalation triggered: fall through to browser tier
            obs.escalation_reason = escalation_reason
            http_error = f"HTTP extraction looked incomplete ({len(extracted['content'])} chars)"
            http_diagnostics = make_diagnostics(
                url,
                'http',
                'extract',
                'invalid_content',
                http_attempted=True,
                http_retry_attempted=bool(
                    http_attempt_state.get('retry_attempted', False)
                ),
            )
        except Exception as exc:
            obs.http_latency_ms = round((time.perf_counter() - http_start) * 1000, 2)
            obs.escalation_reason = "http_error"
            http_error = str(exc)
            http_diagnostics = diagnostics_for_exception(exc) or make_diagnostics(
                url,
                'http',
                'response',
                'unknown',
                http_attempted=True,
            )
            if resolved_render_mode == "http_only":
                obs.failure_reason = http_error
                record_retrieval(obs)
                raise

    # 3. Tier 3: Isolated browser execution
    browser_start = time.perf_counter()
    try:
        page = browser_fetch(url)
        obs.browser_latency_ms = round((time.perf_counter() - browser_start) * 1000, 2)
        obs.retrieval_tier = "browser"
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

        record_retrieval(obs)
        result = shape_output({
            'success': True,
            'requested_url': url,
            'final_url': page['final_url'],
            'status_code': page['status_code'],
            'render_method': 'browser',
            'retrieval_tier': 'browser',
            'cache_hit': False,
            'elapsed_ms': round((time.perf_counter() - started) * 1000),
            'fallback_reason': 'forced' if (force_browser or resolved_render_mode == "browser") else http_error,
            'telemetry': obs.to_dict(),
            **extracted,
        }, max_chars)
        if not no_cache and page.get('status_code') == 200:
            get_cache_provider().set(cache_key, result)
        return result
    except Exception as browser_error:
        obs.browser_latency_ms = round((time.perf_counter() - browser_start) * 1000, 2)
        obs.failure_reason = str(browser_error)
        record_retrieval(obs)
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
