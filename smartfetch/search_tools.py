"""Opt-in web search and search-and-fetch retrieval tools for SmartFetch."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from .batch import batch_fetch_urls
from .cache import build_search_cache_key, get_cache_provider
from .config import SERVICE_VERSION
from .diagnostics import RetrievalFailure
from .security import validate_public_url

_SEARCH_CACHE_TTL_SECONDS = 180
_SERPER_BASE_URL = "https://google.serper.dev"
_EXA_SEARCH_URL = "https://api.exa.ai/search"
_PROVIDER_TIMEOUT_SECONDS = 8.0


def get_serper_api_key() -> str | None:
    """Return trimmed SERPER_API_KEY from env, or None if unset/empty."""
    val = os.environ.get("SERPER_API_KEY", "").strip()
    return val or None


def get_exa_api_key() -> str | None:
    """Return trimmed EXA_API_KEY from env, or None if unset/empty."""
    val = os.environ.get("EXA_API_KEY", "").strip()
    return val or None


def search_configured() -> bool:
    """Return True if at least one search provider key (Serper or Exa) is configured."""
    return bool(get_serper_api_key() or get_exa_api_key())


def leads_configured() -> bool:
    """Return True if Serper API key is configured for lead search."""
    return bool(get_serper_api_key())


def _safe_post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(url, headers=headers, json=payload, timeout=_PROVIDER_TIMEOUT_SECONDS)
        if resp.status_code >= 400:
            raise RetrievalFailure(f"Search provider returned HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RetrievalFailure("Search provider returned non-object JSON")
        return data
    except RetrievalFailure:
        raise
    except Exception as exc:
        raise RetrievalFailure(f"Search provider request failed: {exc}") from exc
    finally:
        session.close()


def _normalize_serper_results(data: dict[str, Any], search_type: str, num_results: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if search_type == "news":
        raw_items = data.get("news") or []
        for idx, item in enumerate(raw_items[:num_results], start=1):
            if not isinstance(item, dict):
                continue
            results.append({
                "position": idx,
                "title": str(item.get("title") or ""),
                "url": str(item.get("link") or ""),
                "snippet": str(item.get("snippet") or ""),
                "source": str(item.get("source") or ""),
                "published_date": str(item.get("date") or ""),
            })
        return results

    if search_type == "places":
        raw_items = data.get("places") or data.get("maps") or []
        for idx, item in enumerate(raw_items[:num_results], start=1):
            if not isinstance(item, dict):
                continue
            item_url = str(item.get("website") or item.get("link") or "").strip()
            item_phone = str(item.get("phoneNumber") or item.get("phone") or "").strip()
            item_reviews = item.get("ratingCount")
            if item_reviews is None:
                item_reviews = item.get("reviews")
            results.append({
                "position": idx,
                "title": str(item.get("title") or ""),
                "url": item_url,
                "snippet": str(item.get("category") or item.get("type") or ""),
                "address": str(item.get("address") or ""),
                "phone": item_phone,
                "rating": item.get("rating"),
                "reviews_count": item_reviews,
                "category": str(item.get("category") or item.get("type") or ""),
            })
        return results

    raw_items = data.get("organic") or []
    for idx, item in enumerate(raw_items[:num_results], start=1):
        if not isinstance(item, dict):
            continue
        results.append({
            "position": idx,
            "title": str(item.get("title") or ""),
            "url": str(item.get("link") or ""),
            "snippet": str(item.get("snippet") or ""),
            "published_date": str(item.get("date") or ""),
        })
    return results


def _normalize_exa_results(data: dict[str, Any], num_results: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    raw_items = data.get("results") or []
    for idx, item in enumerate(raw_items[:num_results], start=1):
        if not isinstance(item, dict):
            continue
        highlights = item.get("highlights")
        snippet = ""
        if isinstance(highlights, list) and highlights:
            snippet = " ... ".join(str(h) for h in highlights if h)[:500]
        elif item.get("summary"):
            snippet = str(item.get("summary"))[:500]
        results.append({
            "position": idx,
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": snippet,
            "published_date": str(item.get("publishedDate") or ""),
        })
    return results


def search_web_query(
    query: str,
    *,
    provider: str = "auto",
    search_type: str = "web",
    num_results: int = 10,
    country: str = "us",
    language: str = "en",
    no_cache: bool = False,
) -> dict[str, Any]:
    """Run an opt-in web/news/places search using Serper first (auto) or Exa fallback."""
    started = time.perf_counter()
    if not isinstance(query, str) or not (1 <= len(query.strip()) <= 500):
        raise ValueError("query must be a string between 1 and 500 characters")
    clean_query = query.strip()

    clean_provider = (provider or "auto").strip().lower()
    if clean_provider not in ("auto", "serper", "exa"):
        raise ValueError("provider must be 'auto', 'serper', or 'exa'")

    clean_type = (search_type or "web").strip().lower()
    if clean_type not in ("web", "news", "places"):
        raise ValueError("search_type must be 'web', 'news', or 'places'")

    clamped_num = max(1, min(10, int(num_results)))
    clean_country = (country or "us").strip().lower()[:8] or "us"
    clean_lang = (language or "en").strip().lower()[:8] or "en"

    serper_key = get_serper_api_key()
    exa_key = get_exa_api_key()

    if clean_provider == "auto":
        if serper_key:
            selected_provider = "serper"
        elif exa_key:
            if clean_type != "web":
                raise RetrievalFailure("search_type 'news' and 'places' require SERPER_API_KEY")
            selected_provider = "exa"
        else:
            raise RetrievalFailure("Search provider is not configured")
    elif clean_provider == "serper":
        if not serper_key:
            raise RetrievalFailure("SERPER_API_KEY is not configured")
        selected_provider = "serper"
    else:
        if not exa_key:
            raise RetrievalFailure("EXA_API_KEY is not configured")
        if clean_type != "web":
            raise ValueError("provider 'exa' only supports search_type='web'")
        selected_provider = "exa"

    cache = get_cache_provider()
    cache_key = build_search_cache_key(
        clean_query,
        provider=selected_provider,
        search_type=clean_type,
        num_results=clamped_num,
        country=clean_country,
        language=clean_lang,
    )
    if not no_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            cached["cached"] = True
            return cached

    if selected_provider == "serper":
        endpoint = "search" if clean_type == "web" else clean_type
        serper_payload = {
            "q": clean_query,
            "num": clamped_num,
            "gl": clean_country,
            "hl": clean_lang,
        }
        raw = _safe_post_json(
            f"{_SERPER_BASE_URL}/{endpoint}",
            headers={"X-API-Key": serper_key or "", "Content-Type": "application/json"},
            payload=serper_payload,
        )
        results = _normalize_serper_results(raw, clean_type, clamped_num)
        if clean_type == "places" and (not results or not any(r.get("url") for r in results)):
            try:
                maps_raw = _safe_post_json(
                    f"{_SERPER_BASE_URL}/maps",
                    headers={"X-API-Key": serper_key or "", "Content-Type": "application/json"},
                    payload=serper_payload,
                )
                maps_results = _normalize_serper_results(maps_raw, clean_type, clamped_num)
                if maps_results and (any(r.get("url") for r in maps_results) or not results):
                    results = maps_results
            except Exception:
                pass
    else:
        raw = _safe_post_json(
            _EXA_SEARCH_URL,
            headers={"x-api-key": exa_key or "", "Content-Type": "application/json"},
            payload={
                "query": clean_query,
                "numResults": clamped_num,
                "contents": {"highlights": {"maxCharacters": 500}},
            },
        )
        results = _normalize_exa_results(raw, clamped_num)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response: dict[str, Any] = {
        "success": True,
        "query": clean_query,
        "provider_used": selected_provider,
        "search_type": clean_type,
        "num_results": len(results),
        "results": results,
        "cached": False,
        "elapsed_ms": elapsed_ms,
        "service_version": SERVICE_VERSION,
    }
    cache.set(cache_key, response, ttl_seconds=_SEARCH_CACHE_TTL_SECONDS)
    return response


def search_and_fetch_query(
    query: str,
    *,
    num_results: int = 3,
    max_chars_per_page: int = 6000,
    provider: str = "auto",
    render_mode: str = "auto",
    no_cache: bool = False,
) -> dict[str, Any]:
    """Search for public URLs and batch-fetch their clean Markdown/content."""
    started = time.perf_counter()
    clamped_num = max(1, min(5, int(num_results)))
    clamped_chars = max(1000, min(15000, int(max_chars_per_page)))
    clean_render_mode = (render_mode or "auto").strip().lower()
    if clean_render_mode not in ("auto", "http_only"):
        clean_render_mode = "auto"

    search_res = search_web_query(
        query,
        provider=provider,
        search_type="web",
        num_results=clamped_num,
        no_cache=no_cache,
    )

    candidate_urls: list[str] = []
    seen: set[str] = set()
    for item in search_res.get("results") or []:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        try:
            validate_public_url(url)
        except Exception:
            continue
        seen.add(url)
        candidate_urls.append(url)
        if len(candidate_urls) >= clamped_num:
            break

    if not candidate_urls:
        raise RetrievalFailure("Search returned no fetchable public URLs")

    batch_res = batch_fetch_urls(
        candidate_urls,
        max_chars_per_url=clamped_chars,
        render_mode=clean_render_mode,
        no_cache=no_cache,
    )
    if int(batch_res.get("succeeded_count") or 0) == 0:
        raise RetrievalFailure("All fetched search result pages failed retrieval")

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "success": True,
        "query": search_res["query"],
        "provider_used": search_res["provider_used"],
        "requested_count": len(candidate_urls),
        "succeeded_count": batch_res["succeeded_count"],
        "failed_count": batch_res["failed_count"],
        "results": batch_res["results"],
        "elapsed_ms": elapsed_ms,
        "service_version": SERVICE_VERSION,
    }
