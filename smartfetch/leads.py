"""Opt-in B2B Enriched Leads search (Serper Maps/Web + Deep /inspect) for SmartFetch."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Any

import requests

from .cache import build_leads_cache_key, get_cache_provider
from .config import SERVICE_VERSION
from .diagnostics import RetrievalFailure
from .inspect import inspect_webpage_url
from .search_tools import get_serper_api_key, leads_configured
from .security import validate_public_url

_LEADS_CACHE_TTL_SECONDS = 180
_SERPER_BASE_URL = "https://google.serper.dev"
_SERPER_TIMEOUT_SECONDS = 8.0
_INSPECT_DEADLINE_SECONDS = 20.0

__all__ = ["leads_configured", "search_leads_query"]


def _fetch_serper_leads(
    query: str,
    *,
    source: str,
    max_leads: int,
    country: str,
    language: str,
) -> list[dict[str, Any]]:
    serper_key = get_serper_api_key()
    if not serper_key:
        raise RetrievalFailure("SERPER_API_KEY is not configured")

    endpoint = "places" if source == "maps" else "search"
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(
            f"{_SERPER_BASE_URL}/{endpoint}",
            headers={"X-API-Key": serper_key, "Content-Type": "application/json"},
            json={
                "q": query,
                "num": max(10, max_leads),
                "gl": country,
                "hl": language,
            },
            timeout=_SERPER_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            raise RetrievalFailure(f"Serper lead query returned HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RetrievalFailure("Serper lead query returned non-object JSON")
    except RetrievalFailure:
        raise
    except Exception as exc:
        raise RetrievalFailure(f"Serper lead query failed: {exc}") from exc
    finally:
        session.close()

    raw_items = data.get("places") if source == "maps" else data.get("organic")
    if not isinstance(raw_items, list) or not raw_items:
        raise RetrievalFailure("No leads found for query")

    leads: list[dict[str, Any]] = []
    for item in raw_items[:max_leads]:
        if not isinstance(item, dict):
            continue
        website = str(item.get("website") or item.get("link") or "").strip()
        leads.append({
            "business_name": str(item.get("title") or ""),
            "category": str(item.get("category") or item.get("type") or ""),
            "rating": item.get("rating"),
            "reviews_count": item.get("ratingCount"),
            "address": str(item.get("address") or ""),
            "maps_phone": str(item.get("phoneNumber") or ""),
            "website": website,
            "inspected": False,
            "emails": [],
            "phones": [],
            "contact_url": None,
            "pricing_url": None,
            "social_links": {},
            "tech_stack": [],
            "has_mx": False,
            "has_spf": False,
            "has_dmarc": False,
            "domain_age_days": None,
            "title": str(item.get("title") or ""),
            "description": str(item.get("snippet") or ""),
        })

    if not leads:
        raise RetrievalFailure("No leads found for query")
    return leads


def _enrich_lead_with_inspect(
    lead: dict[str, Any],
    *,
    deep: bool,
    no_cache: bool,
) -> dict[str, Any]:
    website = str(lead.get("website") or "").strip()
    if not website:
        return lead
    try:
        validate_public_url(website)
        res = inspect_webpage_url(
            website,
            deep=deep,
            render_mode="http_only",
            no_cache=no_cache,
        )
    except Exception:
        return lead

    contacts = res.get("contacts") or {}
    meta = res.get("meta") or {}
    intel = res.get("domain_intel") or {}
    social = res.get("social_links") or {}

    enriched = dict(lead)
    enriched.update({
        "inspected": True,
        "emails": list(contacts.get("emails") or []),
        "phones": list(contacts.get("phones") or []),
        "contact_url": contacts.get("contact_url"),
        "pricing_url": contacts.get("pricing_url"),
        "social_links": dict(social),
        "tech_stack": list(res.get("tech_stack") or []),
        "has_mx": bool(intel.get("has_mx", False)),
        "has_spf": bool(intel.get("has_spf", False)),
        "has_dmarc": bool(intel.get("has_dmarc", False)),
        "domain_age_days": intel.get("domain_age_days"),
        "title": str(meta.get("title") or lead.get("business_name") or ""),
        "description": str(meta.get("description") or lead.get("description") or ""),
    })
    return enriched


def search_leads_query(
    query: str,
    *,
    source: str = "maps",
    max_leads: int = 5,
    deep: bool = True,
    country: str = "us",
    language: str = "en",
    no_cache: bool = False,
    _max_allowed: int = 5,
) -> dict[str, Any]:
    """Find businesses via Serper Places/Search and enrich each with Deep /inspect."""
    started = time.perf_counter()
    if not isinstance(query, str) or not (1 <= len(query.strip()) <= 500):
        raise ValueError("query must be a string between 1 and 500 characters")
    clean_query = query.strip()

    clean_source = (source or "maps").strip().lower()
    if clean_source not in ("maps", "web"):
        raise ValueError("source must be 'maps' or 'web'")

    cap = max(1, int(_max_allowed))
    clamped_max = max(1, min(cap, int(max_leads)))
    clean_country = (country or "us").strip().lower()[:8] or "us"
    clean_lang = (language or "en").strip().lower()[:8] or "en"

    cache = get_cache_provider()
    cache_key = build_leads_cache_key(
        clean_query,
        source=clean_source,
        max_leads=clamped_max,
        deep=bool(deep),
        country=clean_country,
        language=clean_lang,
    )
    if not no_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            cached["cached"] = True
            return cached

    leads = _fetch_serper_leads(
        clean_query,
        source=clean_source,
        max_leads=clamped_max,
        country=clean_country,
        language=clean_lang,
    )

    enriched_leads = list(leads)
    inspect_indices: list[int] = []
    for idx, lead in enumerate(leads):
        website = str(lead.get("website") or "").strip()
        if website:
            try:
                validate_public_url(website)
                inspect_indices.append(idx)
            except Exception:
                pass

    if inspect_indices:
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_idx = {
                executor.submit(_enrich_lead_with_inspect, leads[idx], deep=bool(deep), no_cache=no_cache): idx
                for idx in inspect_indices
            }
            try:
                for future in as_completed(future_to_idx, timeout=_INSPECT_DEADLINE_SECONDS):
                    idx = future_to_idx[future]
                    try:
                        enriched_leads[idx] = future.result()
                    except Exception:
                        pass
            except TimeoutError:
                pass

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response: dict[str, Any] = {
        "success": True,
        "query": clean_query,
        "source": clean_source,
        "total_leads": len(enriched_leads),
        "inspected_count": sum(1 for item in enriched_leads if item.get("inspected")),
        "leads": enriched_leads,
        "cached": False,
        "elapsed_ms": elapsed_ms,
        "service_version": SERVICE_VERSION,
    }
    cache.set(cache_key, response, ttl_seconds=_LEADS_CACHE_TTL_SECONDS)
    return response
