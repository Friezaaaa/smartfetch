"""Opt-in X/Twitter search module powered by twitterapi.io."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

from .cache import build_x_search_cache_key, get_cache_provider
from .config import SERVICE_VERSION
from .diagnostics import RetrievalFailure

_X_SEARCH_CACHE_TTL_SECONDS = 120
_TWITTERAPI_SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
_TWITTERAPI_TIMEOUT_SECONDS = 8.0
_MAX_RESPONSE_BYTES = 1_048_576
_HARD_MAX_TWEETS = 20


def get_twitterapi_key() -> str | None:
    """Return trimmed TWITTERAPI_KEY from env, or None if unset/empty."""
    val = os.getenv("TWITTERAPI_KEY", "").strip()
    return val or None


def x_search_configured() -> bool:
    """Return True if TWITTERAPI_KEY is configured."""
    return bool(get_twitterapi_key())


def search_x_posts_query(
    query: str,
    *,
    query_type: str = "Latest",
    max_tweets: int = 10,
    cursor: str | None = None,
    no_cache: bool = False,
) -> dict[str, Any]:
    """Search X/Twitter posts via twitterapi.io with strict <=20 tweet cost cap."""
    started = time.perf_counter()
    if not isinstance(query, str) or not (1 <= len(query.strip()) <= 500):
        raise ValueError("query must be a string between 1 and 500 characters")
    clean_query = query.strip()

    clean_type = (query_type or "Latest").strip()
    if clean_type not in ("Latest", "Top"):
        raise ValueError("query_type must be 'Latest' or 'Top'")

    clamped_max = max(1, min(_HARD_MAX_TWEETS, int(max_tweets)))
    clean_cursor = cursor.strip() if isinstance(cursor, str) and cursor.strip() else None

    api_key = get_twitterapi_key()
    if not api_key:
        raise RetrievalFailure("TWITTERAPI_KEY is not configured")

    cache = get_cache_provider()
    cache_key = build_x_search_cache_key(
        clean_query,
        query_type=clean_type,
        max_tweets=clamped_max,
        cursor=clean_cursor,
    )
    if not no_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            cached["cached"] = True
            return cached

    params: dict[str, Any] = {
        "query": clean_query,
        "queryType": clean_type,
    }
    if clean_cursor:
        params["cursor"] = clean_cursor

    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.get(
            _TWITTERAPI_SEARCH_URL,
            headers={"X-API-Key": api_key},
            params=params,
            timeout=_TWITTERAPI_TIMEOUT_SECONDS,
            stream=True,
        )
        if resp.status_code >= 400:
            raise RetrievalFailure(f"X search provider returned HTTP {resp.status_code}")

        chunks: list[bytes] = []
        total_bytes = 0
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > _MAX_RESPONSE_BYTES:
                raise RetrievalFailure("X search response exceeded 1 MB size cap")
            chunks.append(chunk)
        raw = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(raw, dict):
            raise RetrievalFailure("X search provider returned non-object JSON")
    except RetrievalFailure:
        raise
    except Exception as exc:
        raise RetrievalFailure(f"X search request failed: {exc}") from exc
    finally:
        session.close()

    raw_tweets = raw.get("tweets") or []
    tweets: list[dict[str, Any]] = []
    for item in raw_tweets[:clamped_max]:
        if not isinstance(item, dict):
            continue
        author = item.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        tweets.append({
            "tweet_id": str(item.get("id") or ""),
            "url": str(item.get("url") or ""),
            "text": str(item.get("text") or ""),
            "created_at": str(item.get("createdAt") or item.get("created_at") or ""),
            "author_username": str(author.get("userName") or author.get("username") or ""),
            "author_name": str(author.get("name") or ""),
            "author_followers": int(author.get("followers") or 0),
            "likes": int(item.get("likeCount") or item.get("likes") or 0),
            "retweets": int(item.get("retweetCount") or item.get("retweets") or 0),
            "replies": int(item.get("replyCount") or item.get("replies") or 0),
            "views": int(item.get("viewCount") or item.get("views") or 0),
        })

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response: dict[str, Any] = {
        "success": True,
        "query": clean_query,
        "query_type": clean_type,
        "count": len(tweets),
        "tweets": tweets,
        "next_cursor": raw.get("next_cursor") or None,
        "has_next_page": bool(raw.get("has_next_page", False)),
        "cached": False,
        "elapsed_ms": elapsed_ms,
        "service_version": SERVICE_VERSION,
    }
    cache.set(cache_key, response, ttl_seconds=_X_SEARCH_CACHE_TTL_SECONDS)
    return response
