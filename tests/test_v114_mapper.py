"""Unit and integration tests for Sprint 1: Site URL Mapper (POST /map & map_website) and realistic Tier 1 HTTP headers."""

from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from smartfetch import server
from smartfetch.cache import get_result_cache
from smartfetch.http_fetch import DEFAULT_USER_AGENT, SESSION
from smartfetch.mapper import map_website_urls
from smartfetch.payments import (
    BASE_MAINNET,
    BASE_SEPOLIA,
    SOLANA_MAINNET,
    X402Settings,
)


class Tier1HttpHeadersTests(unittest.TestCase):
    def test_session_uses_realistic_browser_headers(self):
        ua = SESSION.headers.get("User-Agent", "")
        self.assertEqual(ua, DEFAULT_USER_AGENT)
        self.assertIn("Mozilla/5.0", ua)
        self.assertIn("Chrome/", ua)
        self.assertNotIn("SmartFetch/0.1", ua)
        self.assertIn("text/html", SESSION.headers.get("Accept", ""))
        self.assertIn("en-US", SESSION.headers.get("Accept-Language", ""))
        self.assertEqual(SESSION.headers.get("Upgrade-Insecure-Requests"), "1")


class SiteUrlMapperTests(unittest.TestCase):
    def setUp(self):
        get_result_cache().clear()

    @patch("smartfetch.mapper.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.mapper.http_fetch")
    def test_discovers_urls_from_robots_sitemap_index_and_html_links(
        self,
        mock_http_fetch,
        _mock_validate,
    ):
        def fake_fetch(url: str, **_kwargs):
            if url == "https://example.com/robots.txt":
                return {
                    "status_code": 200,
                    "final_url": url,
                    "html": "User-agent: *\nSitemap: https://example.com/custom-index.xml\n",
                }
            if url == "https://example.com/custom-index.xml":
                return {
                    "status_code": 200,
                    "final_url": url,
                    "html": (
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        "<sitemap><loc>https://example.com/sub-sitemap.xml</loc></sitemap>"
                        "</sitemapindex>"
                    ),
                }
            if url == "https://example.com/sub-sitemap.xml":
                return {
                    "status_code": 200,
                    "final_url": url,
                    "html": (
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        "<url><loc>https://example.com/docs/getting-started</loc></url>"
                        "<url><loc>https://example.com/pricing#tiers</loc></url>"
                        "<url><loc>https://blog.example.com/post-1</loc></url>"
                        "</urlset>"
                    ),
                }
            if url == "https://example.com/sitemap.xml":
                return {"status_code": 404, "final_url": url, "html": "Not Found"}
            if url == "https://example.com":
                return {
                    "status_code": 200,
                    "final_url": "https://example.com/",
                    "html": (
                        "<html><body>"
                        '<a href="/about">About</a>'
                        '<a href="https://example.com/docs/api?utm_source=x">API</a>'
                        '<a href="https://external.org/out">External</a>'
                        "</body></html>"
                    ),
                }
            raise RuntimeError(f"Unexpected URL {url}")

        mock_http_fetch.side_effect = fake_fetch

        res = map_website_urls("https://example.com", no_cache=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["base_domain"], "example.com")
        self.assertGreaterEqual(res["sources"]["sitemap_urls_found"], 2)
        self.assertGreaterEqual(res["sources"]["page_links_found"], 2)
        # Subdomain blog.example.com and external.org should be excluded by default
        self.assertIn("https://example.com/", res["urls"])
        self.assertIn("https://example.com/docs/getting-started", res["urls"])
        self.assertIn("https://example.com/pricing", res["urls"])
        self.assertIn("https://example.com/about", res["urls"])
        self.assertIn("https://example.com/docs/api?utm_source=x", res["urls"])
        self.assertNotIn("https://blog.example.com/post-1", res["urls"])
        self.assertNotIn("https://external.org/out", res["urls"])

        # Now test include_subdomains=True and search filter
        res_sub = map_website_urls(
            "https://example.com",
            search="post",
            include_subdomains=True,
            no_cache=True,
        )
        self.assertEqual(res_sub["urls"], ["https://blog.example.com/post-1"])

    def test_ssrf_blocks_private_target_and_private_sitemap_urls(self):
        with self.assertRaises(ValueError):
            map_website_urls("http://127.0.0.1:8080/", no_cache=True)

        # Ensure malicious private sitemap URL inside robots.txt is skipped and never fetched
        fetched_urls: list[str] = []

        def fake_validate(u: str) -> str:
            if "169.254.169.254" in u or "127.0.0.1" in u:
                raise ValueError("Private/local target blocked")
            return u

        def fake_fetch(u: str, **_kw):
            fetched_urls.append(u)
            if u.endswith("/robots.txt"):
                return {
                    "status_code": 200,
                    "final_url": u,
                    "html": "Sitemap: http://169.254.169.254/latest/meta-data/\n",
                }
            if u.endswith("/sitemap.xml"):
                return {"status_code": 404, "final_url": u, "html": ""}
            return {
                "status_code": 200,
                "final_url": "https://example.com/",
                "html": '<html><body><a href="/safe">Safe</a></body></html>',
            }

        with patch("smartfetch.mapper.validate_public_url", side_effect=fake_validate):
            with patch("smartfetch.mapper.http_fetch", side_effect=fake_fetch):
                res = map_website_urls("https://example.com", no_cache=True)

        self.assertTrue(res["success"])
        self.assertNotIn("http://169.254.169.254/latest/meta-data/", fetched_urls)
        self.assertIn("https://example.com/safe", res["urls"])


class MapEndpointAndMcpIntegrationTests(unittest.TestCase):
    @patch("x402.http.HTTPFacilitatorClient.get_supported")
    def test_post_map_returns_dual_chain_402_challenge_when_payment_enabled(self, get_supported):
        from x402.http.utils import decode_payment_required_header
        from x402.schemas import SupportedKind, SupportedResponse

        get_supported.return_value = SupportedResponse(
            kinds=[
                SupportedKind(x402Version=2, scheme="exact", network=BASE_MAINNET),
                SupportedKind(
                    x402Version=2,
                    scheme="exact",
                    network=SOLANA_MAINNET,
                    extra={"feePayer": "CKPKJWNdJEqa81x7CkZ14BVPiY6y16Sxs7owznqtWYp5"},
                ),
            ]
        )
        settings = X402Settings(
            True,
            "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786",
            "$0.005",
            BASE_MAINNET,
            solana_pay_to="88hE4EB4yZwb4F2arPXcG7efwQyU5FDN26DuVpVKVk2W",
            solana_network=SOLANA_MAINNET,
        )
        with patch.dict(
            "os.environ",
            {
                "BROWSER_WORKER_URL": "https://159.89.40.174:9443",
                "BROWSER_WORKER_HMAC_KEY": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            },
        ):
            app = server.create_app(settings)
            with TestClient(app) as client:
                res = client.post("/map", json={"url": "https://example.com"})
                self.assertEqual(res.status_code, 402)
                challenge = decode_payment_required_header(res.headers.get("PAYMENT-REQUIRED"))
                self.assertEqual(len(challenge.accepts), 2)
                evm_opt = next(opt for opt in challenge.accepts if opt.network == BASE_MAINNET)
                sol_opt = next(opt for opt in challenge.accepts if opt.network == SOLANA_MAINNET)
                self.assertEqual(evm_opt.amount, "5000")
                self.assertEqual(sol_opt.amount, "5000")

    @patch("smartfetch.remote_browser.RemoteBrowserConfig.is_configured", True)
    @patch("smartfetch.server.map_website_urls")
    @patch("smartfetch.mcp_server.map_website_urls")
    def test_post_map_and_mcp_map_website_execute_when_unpaid_mode_for_unit_test(
        self,
        mock_mcp_map,
        mock_server_map,
    ):
        fake_res = {
            "success": True,
            "requested_url": "https://example.com",
            "final_url": "https://example.com/",
            "base_domain": "example.com",
            "search": None,
            "include_subdomains": False,
            "total_discovered": 2,
            "returned_count": 2,
            "max_urls": 50,
            "sources": {
                "sitemap_urls_found": 1,
                "page_links_found": 1,
                "sitemaps_checked": ["https://example.com/sitemap.xml"],
            },
            "urls": ["https://example.com/", "https://example.com/docs"],
            "cache_hit": False,
            "elapsed_ms": 42,
        }
        mock_server_map.return_value = dict(fake_res)
        mock_mcp_map.return_value = dict(fake_res)
        app = server.create_app(X402Settings(False, None, "$0.005", BASE_SEPOLIA))
        with TestClient(app) as client:
            res = client.post("/map", json={"url": "https://example.com", "max_urls": 50})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertTrue(body["success"])
            self.assertEqual(body["returned_count"], 2)

            mcp_res = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "map_website",
                        "arguments": {"url": "https://example.com"},
                    },
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            self.assertEqual(mcp_res.status_code, 200)
            mcp_result = mcp_res.json()["result"]
            self.assertFalse(mcp_result["isError"])
            self.assertEqual(mcp_result["structuredContent"]["base_domain"], "example.com")


if __name__ == "__main__":
    unittest.main()
