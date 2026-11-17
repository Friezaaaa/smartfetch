"""Bounded parallel batch URL retrieval engine for SmartFetch."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import secrets
import threading
import time
from typing import Any

from .config import SERVICE_VERSION
from .core import smart_fetch
from .diagnostics import RetrievalFailure, make_diagnostics
from .security import validate_public_url

_LOGGER = logging.getLogger("smartfetch.batch")


class BatchEscalationLimiter:
    """Thread-safe limiter capping concurrent browser escalations per batch to protect VPS."""

    def __init__(self, max_escalations: int = 2) -> None:
        self.max_escalations = max_escalations
        self.current = 0
        self._lock = threading.Lock()

    def allow_escalation(self) -> bool:
        with self._lock:
            if self.current < self.max_escalations:
                self.current += 1
                return True
            return False


def batch_fetch_urls(
    urls: list[str],
    *,
    max_chars_per_url: int = 8000,
    render_mode: str = "auto",
    no_cache: bool = False,
    timeout_seconds: float = 25.0,
) -> dict[str, Any]:
    """Fetches up to 5 public URLs concurrently with fair settlement and VPS protection.

    - Validates 1 to 5 unique non-empty public URLs.
    - SSRF protection on each URL upfront.
    - Clamps max_chars_per_url between 1000 and 20000.
    - ThreadPoolExecutor with up to 5 workers and ~25s deadline.
    - Limits browser escalations to <= 2 per batch.
    - Raises RetrievalFailure if 0 URLs succeed (triggers 502, prevents x402 settlement).
    """
    started = time.perf_counter()

    if not isinstance(urls, (list, tuple)):
        raise ValueError("urls must be a list of URL strings")

    # Deduplicate preserving order
    unique_urls: list[str] = []
    seen: set[str] = set()
    for item in urls:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Each URL in batch must be a non-empty string")
        clean_url = item.strip()
        if clean_url not in seen:
            seen.add(clean_url)
            unique_urls.append(clean_url)

    if not unique_urls:
        raise ValueError("Batch must contain between 1 and 5 unique URLs")
    if len(unique_urls) > 5:
        raise ValueError(f"Batch cannot exceed 5 unique URLs (received {len(unique_urls)})")

    # Upfront SSRF and scheme validation for each URL
    for u in unique_urls:
        validate_public_url(u)

    # Validate & clamp options
    clamped_max_chars = max(1000, min(20000, int(max_chars_per_url)))
    normalized_render_mode = (render_mode or "auto").lower()
    if normalized_render_mode not in ("auto", "http_only"):
        raise ValueError(f"Invalid render_mode '{render_mode}'. Must be 'auto' or 'http_only'")

    limiter = BatchEscalationLimiter(max_escalations=2)
    results: list[dict[str, Any] | None] = [None] * len(unique_urls)

    def _fetch_worker(idx: int, target_url: str) -> tuple[int, dict[str, Any]]:
        try:
            res = smart_fetch(
                target_url,
                max_chars=clamped_max_chars,
                render_mode=normalized_render_mode,
                no_cache=no_cache,
                _browser_limiter=limiter,
            )
            return idx, res
        except Exception as exc:
            _LOGGER.warning("Batch fetch failed for URL %s: %s", target_url, exc)
            err_diag = getattr(exc, "diagnostics", None)
            diag_dict = err_diag.to_dict() if hasattr(err_diag, "to_dict") else None
            return idx, {
                "success": False,
                "url": target_url,
                "error": str(exc),
                "diagnostics": diag_dict,
            }

    max_workers = min(5, len(unique_urls))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_fetch_worker, idx, u)
            for idx, u in enumerate(unique_urls)
        ]
        deadline = started + timeout_seconds
        for fut in futures:
            remaining = max(0.1, deadline - time.perf_counter())
            try:
                idx, res = fut.result(timeout=remaining)
                results[idx] = res
            except Exception as exc:
                # Find matching index for uncompleted future
                for f_idx, f in enumerate(futures):
                    if f is fut and results[f_idx] is None:
                        results[f_idx] = {
                            "success": False,
                            "url": unique_urls[f_idx],
                            "error": f"Request timed out or failed: {exc}",
                        }

    # Fill any remaining unfilled results
    for idx, r in enumerate(results):
        if r is None:
            results[idx] = {
                "success": False,
                "url": unique_urls[idx],
                "error": "Batch retrieval timed out",
            }

    final_results: list[dict[str, Any]] = [r for r in results if r is not None]
    succeeded_count = sum(1 for r in final_results if r.get("success"))
    failed_count = sum(1 for r in final_results if not r.get("success"))

    if succeeded_count == 0:
        err_msgs = [f"{r.get('url')}: {r.get('error')}" for r in final_results]
        diag = make_diagnostics(
            unique_urls[0],
            'http',
            'response',
            'unknown',
            http_attempted=True,
        )
        raise RetrievalFailure(
            f"All {len(final_results)} URLs failed in batch retrieval: {'; '.join(err_msgs)}",
            diag,
        )

    return {
        "success": True,
        "results": final_results,
        "total_urls": len(final_results),
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "request_id": secrets.token_hex(8),
        "service_version": SERVICE_VERSION,
    }
