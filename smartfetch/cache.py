"""Result Cache abstraction and in-memory TTL implementation for SmartFetch."""

from abc import ABC, abstractmethod
import hashlib
import os
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DEFAULT_CACHE_TTL_SECONDS = int(os.getenv("RESULT_CACHE_TTL_SECONDS", "60"))
DEFAULT_CACHE_MAX_SIZE = int(os.getenv("RESULT_CACHE_MAX_SIZE", "1000"))


def normalize_cache_url(url: str) -> str:
    """Produces a canonical URL representation for deterministic cache keys."""
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return url.strip()

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # Strip standard port suffixes
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Sort query parameters
    query_params = parse_qsl(parsed.query, keep_blank_values=True)
    query_params.sort(key=lambda item: item[0])
    query = urlencode(query_params)

    # Exclude fragment from cache key
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def build_cache_key(
    url: str,
    format: str = "markdown",
    render_mode: str = "auto",
    include_html: bool = False,
    only_main_content: bool = True,
) -> str:
    """Builds a normalized SHA-256 cache key scoped by URL, format, and render mode."""
    canonical_url = normalize_cache_url(url)
    url_digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]
    fmt = (format or "markdown").lower()
    mode = (render_mode or "auto").lower()
    base = f"sf:{url_digest}:{fmt}:{mode}"
    if include_html:
        base = f"{base}:html"
    if not only_main_content:
        base = f"{base}:full"
    return base


def build_screenshot_cache_key(
    url: str,
    viewport: str = "desktop",
    full_page: bool = False,
    image_format: str = "jpeg",
) -> str:
    """Builds a normalized SHA-256 cache key scoped by URL, viewport, full_page, and format."""
    canonical_url = normalize_cache_url(url)
    url_digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]
    vp = (viewport or "desktop").lower()
    fp = "full" if full_page else "viewport"
    fmt = (image_format or "jpeg").lower()
    return f"sf:screenshot:{url_digest}:{vp}:{fp}:{fmt}"


def build_inspect_cache_key(
    url: str,
    render_mode: str = "auto",
) -> str:
    """Builds a normalized SHA-256 cache key scoped by URL and render mode for inspect."""
    canonical_url = normalize_cache_url(url)
    url_digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]
    mode = (render_mode or "auto").lower()
    return f"sf:inspect:{url_digest}:{mode}"


def build_map_cache_key(
    url: str,
    search: str | None = None,
    include_subdomains: bool = False,
    max_urls: int = 200,
) -> str:
    """Builds a normalized SHA-256 cache key scoped by URL, search filter, subdomains, and max_urls."""
    canonical_url = normalize_cache_url(url)
    search_norm = (search or "").strip().lower()
    sub_flag = "sub" if include_subdomains else "exact"
    limit = max(1, min(500, int(max_urls or 200)))
    raw = f"{canonical_url}|{search_norm}|{sub_flag}|{limit}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"sf:map:{digest}"


class ResultCacheProvider(ABC):
    """Abstract protocol for retrieval result cache backends (In-memory, Redis, Valkey, etc.)."""

    @abstractmethod
    def get(self, key: str) -> dict[str, Any] | None:
        """Retrieve a cached extraction result, or None if expired/absent."""
        pass

    @abstractmethod
    def set(self, key: str, value: dict[str, Any], ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        """Store an extraction result with a time-to-live in seconds."""
        pass

    @abstractmethod
    def delete(self, key: str) -> None:
        """Explicitly evict an entry from the cache."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Purge all entries from the cache."""
        pass


class InMemoryTTLCacheProvider(ResultCacheProvider):
    """Thread-safe in-memory cache provider with monotonic timestamp TTL expiration."""

    def __init__(self, maxsize: int = DEFAULT_CACHE_MAX_SIZE) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._store: dict[str, tuple[dict[str, Any], float]] = {}

    def _purge_expired(self, now: float) -> None:
        expired_keys = [k for k, (_, expires_at) in self._store.items() if expires_at <= now]
        for k in expired_keys:
            self._store.pop(k, None)

    def get(self, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at <= now:
                self._store.pop(key, None)
                return None
            return dict(value)

    def set(self, key: str, value: dict[str, Any], ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        now = time.monotonic()
        expires_at = now + max(1, ttl_seconds)
        stored_value = dict(value)
        with self._lock:
            if len(self._store) >= self._maxsize:
                self._purge_expired(now)
                # If still at capacity, evict oldest entry
                if len(self._store) >= self._maxsize:
                    oldest_key = min(self._store, key=lambda k: self._store[k][1])
                    self._store.pop(oldest_key, None)
            self._store[key] = (stored_value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        now = time.monotonic()
        with self._lock:
            self._purge_expired(now)
            return len(self._store)


# Global active provider instance
_ACTIVE_CACHE_PROVIDER: ResultCacheProvider = InMemoryTTLCacheProvider()


def get_cache_provider() -> ResultCacheProvider:
    return _ACTIVE_CACHE_PROVIDER


get_result_cache = get_cache_provider


def set_cache_provider(provider: ResultCacheProvider) -> None:
    global _ACTIVE_CACHE_PROVIDER
    if not isinstance(provider, ResultCacheProvider):
        raise TypeError("provider must implement ResultCacheProvider")
    _ACTIVE_CACHE_PROVIDER = provider
