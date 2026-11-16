"""Tests for v1.13 Phase 2: Tiered Retrieval Waterfall, Result Cache, and Observability."""

import time
import unittest
from unittest.mock import MagicMock, patch

from smartfetch.cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    InMemoryTTLCacheProvider,
    ResultCacheProvider,
    build_cache_key,
    get_cache_provider,
    normalize_cache_url,
    set_cache_provider,
)
from smartfetch.core import evaluate_escalation_heuristics, smart_fetch
from smartfetch.observability import (
    ObservabilityRecord,
    TelemetryRegistry,
    get_telemetry_registry,
    get_telemetry_snapshot,
    reset_telemetry,
)


class TestResultCacheProvider(unittest.TestCase):
    """Verifies the ResultCacheProvider interface and InMemoryTTLCacheProvider."""

    def setUp(self):
        self.cache = InMemoryTTLCacheProvider(maxsize=100)

    def test_set_and_get_cache_hit(self):
        self.cache.set("key1", {"content": "cached data", "success": True}, ttl_seconds=60)
        res = self.cache.get("key1")
        self.assertIsNotNone(res)
        self.assertEqual(res["content"], "cached data")

    def test_cache_miss_returns_none(self):
        self.assertIsNone(self.cache.get("nonexistent"))

    def test_ttl_expiration_purges_entry(self):
        self.cache.set("short_lived", {"foo": "bar"}, ttl_seconds=1)
        self.assertIsNotNone(self.cache.get("short_lived"))
        time.sleep(1.1)
        self.assertIsNone(self.cache.get("short_lived"))

    def test_delete_and_clear(self):
        self.cache.set("k1", {"val": 1})
        self.cache.set("k2", {"val": 2})
        self.cache.delete("k1")
        self.assertIsNone(self.cache.get("k1"))
        self.assertIsNotNone(self.cache.get("k2"))
        self.cache.clear()
        self.assertEqual(self.cache.size(), 0)

    def test_custom_provider_registration(self):
        class MockCustomProvider(ResultCacheProvider):
            def __init__(self):
                self.items = {}
            def get(self, key):
                return self.items.get(key)
            def set(self, key, value, ttl_seconds=60):
                self.items[key] = value
            def delete(self, key):
                self.items.pop(key, None)
            def clear(self):
                self.items.clear()

        orig = get_cache_provider()
        try:
            custom = MockCustomProvider()
            set_cache_provider(custom)
            self.assertIs(get_cache_provider(), custom)
            with self.assertRaises(TypeError):
                set_cache_provider("not-a-provider")
        finally:
            set_cache_provider(orig)

    def test_cache_key_normalization(self):
        # Case insensitive host, port stripping, query sorting, fragment stripping
        url1 = "https://EXAMPLE.com:443/docs/?b=2&a=1#section"
        url2 = "https://example.com/docs?a=1&b=2"
        self.assertEqual(normalize_cache_url(url1), normalize_cache_url(url2))
        key1 = build_cache_key(url1, format="markdown", render_mode="auto")
        key2 = build_cache_key(url2, format="markdown", render_mode="auto")
        self.assertEqual(key1, key2)


class TestObservability(unittest.TestCase):
    """Verifies telemetry metrics recording and snapshots."""

    def setUp(self):
        reset_telemetry()

    def test_telemetry_registry_records_counters(self):
        registry = get_telemetry_registry()
        registry.record(ObservabilityRecord(retrieval_tier="http", cache_hit=False, http_latency_ms=45.0))
        registry.record(ObservabilityRecord(retrieval_tier="cache", cache_hit=True))
        registry.record(ObservabilityRecord(retrieval_tier="browser", escalation_reason="spa_root_tag", browser_latency_ms=1200.0))
        registry.record(ObservabilityRecord(retrieval_tier="http", failure_reason="dns_error"))

        snap = get_telemetry_snapshot()
        self.assertEqual(snap["counters"]["total_requests"], 4)
        self.assertEqual(snap["counters"]["cache_hits"], 1)
        self.assertEqual(snap["counters"]["cache_misses"], 3)
        self.assertEqual(snap["counters"]["tier_http"], 2)
        self.assertEqual(snap["counters"]["tier_cache"], 1)
        self.assertEqual(snap["counters"]["tier_browser"], 1)
        self.assertEqual(snap["counters"]["escalations"], 1)
        self.assertEqual(snap["counters"]["failures"], 1)


class TestTieredWaterfall(unittest.TestCase):
    """Verifies Tier 1 HTTP, Tier 2 Heuristics, Tier 3 Browser, and Caching waterfall dispatch."""

    def setUp(self):
        reset_telemetry()
        get_cache_provider().clear()

    @patch("smartfetch.core.http_fetch")
    @patch("smartfetch.core.browser_fetch")
    def test_tier1_static_http_success_does_not_call_browser(self, mock_browser, mock_http):
        mock_http.return_value = {
            "html": "<html><body><h1>Article</h1><p>" + ("Solid documentation text. " * 30) + "</p></body></html>",
            "final_url": "https://example.com/article",
            "status_code": 200,
        }

        res = smart_fetch("https://example.com/article")
        self.assertTrue(res["success"])
        self.assertEqual(res["render_method"], "http")
        self.assertEqual(res["retrieval_tier"], "http")
        self.assertFalse(res["cache_hit"])
        mock_http.assert_called_once()
        mock_browser.assert_not_called()

        # Telemetry check
        snap = get_telemetry_snapshot()
        self.assertEqual(snap["counters"]["tier_http"], 1)
        self.assertEqual(snap["counters"]["cache_hits"], 0)

    @patch("smartfetch.core.http_fetch")
    @patch("smartfetch.core.browser_fetch")
    def test_cache_hit_bypasses_http_and_browser(self, mock_browser, mock_http):
        mock_http.return_value = {
            "html": "<html><body><h1>Doc</h1><p>" + ("Documentation content. " * 30) + "</p></body></html>",
            "final_url": "https://example.com/doc",
            "status_code": 200,
        }

        # First call populates cache
        res1 = smart_fetch("https://example.com/doc")
        self.assertEqual(res1["retrieval_tier"], "http")
        self.assertEqual(mock_http.call_count, 1)

        # Second call hits cache
        res2 = smart_fetch("https://example.com/doc")
        self.assertTrue(res2["success"])
        self.assertEqual(res2["retrieval_tier"], "cache")
        self.assertTrue(res2["cache_hit"])
        # Neither HTTP nor Browser was called again!
        self.assertEqual(mock_http.call_count, 1)
        mock_browser.assert_not_called()

        # Client Cache-Control: no-cache bypasses cache
        res3 = smart_fetch("https://example.com/doc", no_cache=True)
        self.assertEqual(res3["retrieval_tier"], "http")
        self.assertEqual(mock_http.call_count, 2)

    @patch("smartfetch.core.http_fetch")
    @patch("smartfetch.core.browser_fetch")
    def test_tier2_escalates_on_spa_root(self, mock_browser, mock_http):
        mock_http.return_value = {
            "html": "<html><head><title>App</title></head><body><div id='root'></div><script src='bundle.js'></script></body></html>",
            "final_url": "https://app.example.com/",
            "status_code": 200,
        }
        mock_browser.return_value = {
            "html": "<html><body><div id='root'><h1>Rendered Dashboard</h1><p>" + ("Dashboard widget data. " * 20) + "</p></div></body></html>",
            "final_url": "https://app.example.com/dashboard",
            "status_code": 200,
        }

        res = smart_fetch("https://app.example.com/")
        self.assertTrue(res["success"])
        self.assertEqual(res["render_method"], "browser")
        self.assertEqual(res["retrieval_tier"], "browser")
        mock_http.assert_called_once()
        mock_browser.assert_called_once()

        snap = get_telemetry_snapshot()
        self.assertEqual(snap["counters"]["tier_browser"], 1)
        self.assertEqual(snap["counters"]["escalations"], 1)
        self.assertEqual(snap["last_record"]["escalation_reason"], "spa_root_tag")

    @patch("smartfetch.core.http_fetch")
    @patch("smartfetch.core.browser_fetch")
    def test_tier2_escalates_on_noscript_js_warning(self, mock_browser, mock_http):
        mock_http.return_value = {
            "html": "<html><body><noscript>You need to enable JavaScript to run this app.</noscript></body></html>",
            "final_url": "https://spa.example.com/",
            "status_code": 200,
        }
        mock_browser.return_value = {
            "html": "<html><body><main><h1>Full Client Render</h1><p>" + ("Real text. " * 30) + "</p></main></body></html>",
            "final_url": "https://spa.example.com/app",
            "status_code": 200,
        }

        res = smart_fetch("https://spa.example.com/")
        self.assertEqual(res["render_method"], "browser")
        self.assertEqual(res["telemetry"]["escalation_reason"], "noscript_warning")

    @patch("smartfetch.core.http_fetch")
    @patch("smartfetch.core.browser_fetch")
    def test_render_mode_http_only_never_escalates(self, mock_browser, mock_http):
        mock_http.return_value = {
            "html": "<html><body><div id='root'></div></body></html>",
            "final_url": "https://raw.example.com/",
            "status_code": 200,
        }

        res = smart_fetch("https://raw.example.com/", render_mode="http_only")
        self.assertEqual(res["render_method"], "http")
        self.assertEqual(res["retrieval_tier"], "http")
        mock_http.assert_called_once()
        mock_browser.assert_not_called()

    @patch("smartfetch.core.http_fetch")
    @patch("smartfetch.core.browser_fetch")
    def test_render_mode_browser_skips_http(self, mock_browser, mock_http):
        mock_browser.return_value = {
            "html": "<html><body><h1>Browser Forced</h1><p>" + ("Client content. " * 20) + "</p></body></html>",
            "final_url": "https://forced.example.com/",
            "status_code": 200,
        }

        res = smart_fetch("https://forced.example.com/", render_mode="browser")
        self.assertEqual(res["render_method"], "browser")
        self.assertEqual(res["retrieval_tier"], "browser")
        self.assertEqual(res["fallback_reason"], "forced")
        mock_http.assert_not_called()
        mock_browser.assert_called_once()


if __name__ == "__main__":
    unittest.main()
