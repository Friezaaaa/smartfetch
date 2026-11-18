"""Site URL Mapper (robots.txt + sitemap.xml + HTML link discovery) for SmartFetch."""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .browser_fetch import browser_fetch
from .cache import build_map_cache_key, get_result_cache
from .config import SERVICE_VERSION
from .core import NOSCRIPT_JS_PATTERN, SPA_ROOT_PATTERNS
from .diagnostics import (
    RetrievalDiagnostics,
    RetrievalFailure,
    diagnostics_for_exception,
    make_diagnostics,
    normalize_target_host,
)
from .http_fetch import http_fetch
from .security import validate_public_url

_LOGGER = logging.getLogger("smartfetch.mapper")

MAX_SITEMAP_BYTES = 5 * 1024 * 1024  # 5 MB per XML
MAX_NESTED_SITEMAPS = 5
MAP_WALL_CLOCK_BUDGET_SECONDS = 11.5

_ROBOTS_SITEMAP_RE = re.compile(r"^\s*Sitemap:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_www(host: str) -> str:
    h = (host or "").strip().lower()
    if ":" in h:
        h = h.split(":", 1)[0]
    if h.startswith("www.") and len(h) > 4:
        return h[4:]
    return h


def _host_matches(candidate_netloc: str, base_netloc: str, include_subdomains: bool) -> bool:
    cand_host = (candidate_netloc or "").strip().lower()
    target_host = (base_netloc or "").strip().lower()
    if ":" in cand_host:
        cand_host = cand_host.split(":", 1)[0]
    if ":" in target_host:
        target_host = target_host.split(":", 1)[0]
    if not cand_host or not target_host:
        return False

    cand_bare = _strip_www(cand_host)
    target_bare = _strip_www(target_host)
    if cand_host == target_host or cand_bare == target_bare:
        return True
    if include_subdomains and cand_bare.endswith("." + target_bare):
        return True
    return False


def _normalize_discovered_url(raw_url: str, base_url: str) -> str | None:
    if not isinstance(raw_url, str):
        return None
    cleaned = raw_url.strip()
    if not cleaned or cleaned.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    try:
        resolved = urljoin(base_url, cleaned)
        parsed = urlparse(resolved)
    except Exception:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.netloc:
        return None
    netloc = parsed.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def _parse_sitemap_xml(xml_text: str) -> tuple[list[str], list[str]]:
    """Parses XML text into (page_urls, child_sitemap_urls) supporting namespace-agnostic <urlset> and <sitemapindex>."""
    page_urls: list[str] = []
    child_sitemaps: list[str] = []
    if not xml_text or len(xml_text.encode("utf-8", errors="ignore")) > MAX_SITEMAP_BYTES:
        return page_urls, child_sitemaps
    try:
        root = ElementTree.fromstring(xml_text.strip())
    except Exception:
        return page_urls, child_sitemaps

    root_tag = root.tag.split("}")[-1].lower()
    if root_tag == "sitemapindex":
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            if tag == "loc" and elem.text and elem.text.strip():
                child_sitemaps.append(elem.text.strip())
    elif root_tag == "urlset":
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            if tag == "loc" and elem.text and elem.text.strip():
                page_urls.append(elem.text.strip())
    return page_urls, child_sitemaps


def map_website_urls(
    url: str,
    *,
    search: str | None = None,
    include_subdomains: bool = False,
    max_urls: int = 200,
    no_cache: bool = False,
) -> dict[str, Any]:
    """Discovers and maps URLs on a website using robots.txt, sitemaps, and HTML links."""
    started_at = time.perf_counter()
    deadline = started_at + MAP_WALL_CLOCK_BUDGET_SECONDS

    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    clean_url = url.strip()
    validate_public_url(clean_url)

    clamped_max_urls = max(1, min(500, int(max_urls if max_urls is not None else 200)))
    search_clean = (search or "").strip() if isinstance(search, str) else ""

    cache_key = build_map_cache_key(
        clean_url,
        search=search_clean or None,
        include_subdomains=bool(include_subdomains),
        max_urls=clamped_max_urls,
    )
    if not no_cache:
        cached = get_result_cache().get(cache_key)
        if cached is not None and cached.get("success"):
            cached_res = dict(cached)
            cached_res["cache_hit"] = True
            cached_res["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000)
            return cached_res

    parsed_start = urlparse(clean_url)
    origin = f"{parsed_start.scheme.lower()}://{parsed_start.netloc.lower()}"
    base_netloc = parsed_start.netloc.lower()

    sitemap_candidates: list[str] = []
    seen_sitemap_urls: set[str] = set()
    sitemaps_checked: list[str] = []
    sitemap_discovered: list[str] = []
    page_discovered: list[str] = []

    # 1. Check robots.txt for Sitemap: directives
    robots_url = f"{origin}/robots.txt"
    if time.perf_counter() < deadline:
        try:
            validate_public_url(robots_url)
            robots_res = http_fetch(robots_url)
            robots_txt = robots_res.get("html") or ""
            for match in _ROBOTS_SITEMAP_RE.findall(robots_txt):
                sm_url = match.strip()
                if sm_url and sm_url not in seen_sitemap_urls:
                    seen_sitemap_urls.add(sm_url)
                    sitemap_candidates.append(sm_url)
        except Exception as exc:
            _LOGGER.debug("robots.txt lookup skipped for %s: %s", robots_url, exc)

    default_sitemap = f"{origin}/sitemap.xml"
    if default_sitemap not in seen_sitemap_urls:
        seen_sitemap_urls.add(default_sitemap)
        sitemap_candidates.append(default_sitemap)

    # 2. Fetch and parse sitemaps (including up to MAX_NESTED_SITEMAPS child sitemaps)
    queue = list(sitemap_candidates[: MAX_NESTED_SITEMAPS + 1])
    nested_count = 0
    while queue and len(sitemaps_checked) < (MAX_NESTED_SITEMAPS + 2) and time.perf_counter() < deadline:
        sm_target = queue.pop(0)
        try:
            validate_public_url(sm_target)
            sm_res = http_fetch(sm_target)
            sm_xml = sm_res.get("html") or ""
            if len(sm_xml.encode("utf-8", errors="ignore")) > MAX_SITEMAP_BYTES:
                continue
            sitemaps_checked.append(sm_res.get("final_url", sm_target))
            locs, children = _parse_sitemap_xml(sm_xml)
            sitemap_discovered.extend(locs)
            for child in children:
                if nested_count >= MAX_NESTED_SITEMAPS:
                    break
                if child not in seen_sitemap_urls:
                    try:
                        validate_public_url(child)
                    except Exception:
                        continue
                    seen_sitemap_urls.add(child)
                    queue.append(child)
                    nested_count += 1
        except Exception as exc:
            _LOGGER.debug("Sitemap fetch skipped for %s: %s", sm_target, exc)

    # 3. Fetch starting page HTML & extract <a href> links (with SPA shell browser fallback)
    final_url = clean_url
    render_method = "http"
    root_fetch_error: Exception | None = None
    html = ""

    if time.perf_counter() < deadline:
        try:
            page_res = http_fetch(clean_url)
            final_url = page_res.get("final_url", clean_url)
            base_netloc = urlparse(final_url).netloc.lower() or base_netloc
            html = page_res.get("html") or ""

            # Check if starting page is an empty SPA shell needing browser escalation
            if html and time.perf_counter() < deadline:
                needs_escalation = any(p.search(html) for p in SPA_ROOT_PATTERNS) or bool(
                    NOSCRIPT_JS_PATTERN.search(html)
                )
                if not needs_escalation and len(html.strip()) < 150 and "<script" in html.lower():
                    needs_escalation = True
                if needs_escalation:
                    try:
                        b_res = browser_fetch(clean_url)
                        html = b_res.get("html") or html
                        final_url = b_res.get("final_url", final_url)
                        base_netloc = urlparse(final_url).netloc.lower() or base_netloc
                        render_method = "browser"
                    except Exception as b_exc:
                        _LOGGER.debug("Browser escalation skipped during map for %s: %s", clean_url, b_exc)
        except Exception as exc:
            root_fetch_error = exc
            # Try browser fallback if HTTP failed and time remains
            if time.perf_counter() < deadline:
                try:
                    b_res = browser_fetch(clean_url)
                    html = b_res.get("html") or ""
                    final_url = b_res.get("final_url", clean_url)
                    base_netloc = urlparse(final_url).netloc.lower() or base_netloc
                    render_method = "browser"
                    root_fetch_error = None
                except Exception as b_exc:
                    _LOGGER.debug("Browser fallback failed during map for %s: %s", clean_url, b_exc)

    if html:
        try:
            soup = BeautifulSoup(html, "lxml")
            for a_tag in soup.find_all("a", href=True):
                href = a_tag.get("href")
                if isinstance(href, str) and href.strip():
                    page_discovered.append(href.strip())
        except Exception as exc:
            _LOGGER.debug("HTML link parsing failed for %s: %s", final_url, exc)

    # Combine, normalize, filter by domain & search, and deduplicate
    raw_candidates: list[str] = []
    if root_fetch_error is None:
        raw_candidates.append(final_url)
    raw_candidates.extend(sitemap_discovered)
    raw_candidates.extend(page_discovered)

    seen_normalized: set[str] = set()
    matched_urls: list[str] = []
    search_lower = search_clean.lower()

    for candidate in raw_candidates:
        norm = _normalize_discovered_url(candidate, final_url)
        if not norm or norm in seen_normalized:
            continue
        parsed_cand = urlparse(norm)
        if not _host_matches(parsed_cand.netloc, base_netloc, bool(include_subdomains)):
            continue
        seen_normalized.add(norm)
        if search_lower and search_lower not in norm.lower():
            continue
        matched_urls.append(norm)

    if not matched_urls and root_fetch_error is not None:
        diag = diagnostics_for_exception(root_fetch_error) or RetrievalDiagnostics(
            target_host=normalize_target_host(clean_url),
            strategy="http",
            phase="response",
            failure_code="invalid_content",
            http_attempted=True,
            http_retry_attempted=False,
            browser_attempted=(render_method == "browser"),
            fallback_attempted=False,
        )
        raise RetrievalFailure(f"Website mapping failed for {clean_url}: {root_fetch_error}", diag) from root_fetch_error

    capped_urls = matched_urls[:clamped_max_urls]
    result = {
        "success": True,
        "requested_url": clean_url,
        "final_url": final_url,
        "base_domain": _strip_www(base_netloc),
        "urls": capped_urls,
        "total_discovered": len(matched_urls),
        "returned_count": len(capped_urls),
        "max_urls": clamped_max_urls,
        "include_subdomains": bool(include_subdomains),
        "search": search_clean or None,
        "sources": {
            "sitemap_urls_found": len(sitemap_discovered),
            "page_links_found": len(page_discovered),
            "sitemaps_checked": sitemaps_checked,
        },
        "render_method": render_method,
        "cache_hit": False,
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
        "request_id": secrets.token_hex(8),
        "service_version": SERVICE_VERSION,
    }

    if not no_cache:
        get_result_cache().set(cache_key, result, ttl_seconds=60)

    return result
