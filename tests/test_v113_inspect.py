"""Comprehensive test suite for Build 2: Structured Site Intelligence (POST /inspect & inspect_webpage MCP tool)."""

import base64
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import (
    bazaar,
    cache,
    discovery,
    mcp_server,
    server,
)
from smartfetch.cache import build_inspect_cache_key, get_cache_provider
from smartfetch.inspect import inspect_webpage_url
from smartfetch.mcp_server import create_smartfetch_mcp
from smartfetch.payments import BASE_MAINNET, BASE_SEPOLIA, X402Settings
from smartfetch.remote_browser import RemoteBrowserConfig

VALID_PAY_TO = "0x1111111111111111111111111111111111111111"
SUPPORTED_SEPOLIA = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme="exact",
    network=BASE_SEPOLIA,
)])


class InspectCacheKeyTests(unittest.TestCase):
    def test_build_inspect_cache_key(self):
        k1 = build_inspect_cache_key("https://example.com/page", render_mode="auto")
        k2 = build_inspect_cache_key("https://example.com/page", render_mode="browser")
        k3 = build_inspect_cache_key("https://example.com/page?b=2&a=1", render_mode="auto")
        k4 = build_inspect_cache_key("https://example.com/page?a=1&b=2", render_mode="auto")

        self.assertTrue(k1.startswith("sf:inspect:"))
        self.assertTrue(k1.endswith(":auto"))
        self.assertTrue(k2.endswith(":browser"))
        self.assertNotEqual(k1, k2)
        # Query normalization should make k3 and k4 match
        self.assertEqual(k3, k4)


class InspectExtractionTests(unittest.TestCase):
    def test_extract_html_metadata_and_tech_stack(self):
        html_doc = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Acme Corp | Modern SaaS</title>
    <meta name="description" content="Acme provides cloud intelligence.">
    <meta name="generator" content="Next.js">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://acme.com/home">
    <link rel="icon" href="/favicon.ico">
    <meta property="og:title" content="Acme SaaS Platform">
    <meta property="og:description" content="Scale faster with Acme.">
    <meta property="og:image" content="https://acme.com/og.png">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:site" content="@acmecorp">
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "Organization", "name": "Acme Corp"}
    </script>
    <script type="application/ld+json">
    INVALID_JSON_HERE_SHOULD_BE_SKIPPED
    </script>
    <script src="/_next/static/chunks/main.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://js.stripe.com/v3/"></script>
</head>
<body class="bg-gray-100 antialiased font-sans">
    <h1>Welcome to Acme</h1>
    <h2>Cloud Intelligence at Scale</h2>
    <h2>Seamless Integration</h2>
    <p>Contact us at <a href="mailto:info@acme.com">info@acme.com</a> or call <a href="tel:+1-555-0199">+1-555-0199</a>.</p>
    <p>Noise email image: badge@2x.png and logo@preview.jpg should be ignored.</p>
    <div>
        <a href="https://github.com/acmecorp/sdk">GitHub</a>
        <a href="https://twitter.com/acmecorp">Twitter</a>
        <a href="https://linkedin.com/company/acme">LinkedIn</a>
        <a href="/pricing">Internal Pricing</a>
        <a href="/whitepaper.pdf">Download PDF</a>
        <a href="/sitemap.xml">Sitemap</a>
    </div>
</body>
</html>"""

        mock_http_response = {
            "success": True,
            "requested_url": "https://acme.com",
            "final_url": "https://acme.com/home",
            "status_code": 200,
            "render_method": "http",
            "content": "Welcome to Acme",
            "title": "Acme Corp | Modern SaaS",
            "raw_html": html_doc,
            "content_type": "text/html; charset=utf-8",
            "headers": {
                "server": "cloudflare",
                "strict-transport-security": "max-age=31536000; includeSubDomains",
                "content-security-policy": "default-src 'self'",
                "x-frame-options": "DENY",
                "x-content-type-options": "nosniff",
                "set-cookie": "secret_session=xyz123; HttpOnly",
            },
        }

        with patch("smartfetch.inspect.validate_public_url", return_value="https://acme.com"), \
             patch("smartfetch.inspect.http_fetch", return_value=mock_http_response):
            result = inspect_webpage_url("https://acme.com", no_cache=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["retrieval_tier"], "http")
        self.assertEqual(result["render_method"], "http")
        self.assertFalse(result["cache_hit"])

        # Metadata
        meta = result["metadata"]
        self.assertEqual(meta["title"], "Acme Corp | Modern SaaS")
        self.assertEqual(meta["description"], "Acme provides cloud intelligence.")
        self.assertEqual(meta["canonical_url"], "https://acme.com/home")
        self.assertEqual(meta["language"], "en")
        self.assertEqual(meta["robots_meta"], "index, follow")
        self.assertEqual(meta["open_graph"]["title"], "Acme SaaS Platform")
        self.assertEqual(meta["twitter_card"]["card"], "summary_large_image")

        # JSON-LD (valid parsed, invalid skipped)
        self.assertEqual(len(result["json_ld"]), 1)
        self.assertEqual(result["json_ld"][0]["name"], "Acme Corp")

        # Contacts
        contacts = result["contacts"]
        self.assertIn("info@acme.com", contacts["emails"])
        self.assertNotIn("badge@2x.png", contacts["emails"])
        self.assertNotIn("logo@preview.jpg", contacts["emails"])
        self.assertIn("+1-555-0199", contacts["phones"])
        self.assertEqual(contacts["social_links"]["github"], "https://github.com/acmecorp/sdk")
        self.assertEqual(contacts["social_links"]["x_twitter"], "https://twitter.com/acmecorp")
        self.assertEqual(contacts["social_links"]["linkedin"], "https://linkedin.com/company/acme")

        # Tech Stack
        tech = result["tech_stack"]
        self.assertIn("Next.js", tech)
        self.assertIn("React", tech)
        self.assertIn("Tailwind CSS", tech)
        self.assertIn("Cloudflare", tech)
        self.assertIn("Stripe", tech)

        # Security Headers (Set-Cookie MUST be omitted)
        sec = result["security_headers"]
        self.assertTrue(sec["https"])
        self.assertEqual(sec["server"], "cloudflare")
        self.assertEqual(sec["x_frame_options"], "DENY")
        self.assertNotIn("set_cookie", sec)
        self.assertNotIn("set-cookie", sec)

        # Structure
        struct = result["structure"]
        self.assertEqual(struct["h1"], ["Welcome to Acme"])
        self.assertEqual(len(struct["h2"]), 2)
        self.assertEqual(struct["pdf_links"], 1)
        self.assertEqual(struct["sitemap_links"], ["https://acme.com/sitemap.xml"])

    def test_spa_shell_escalation_to_browser(self):
        spa_html = """<!DOCTYPE html>
<html>
<head><title>Loading React App...</title></head>
<body>
    <div id="root"></div>
    <noscript>You need to enable JavaScript to run this app.</noscript>
</body>
</html>"""

        mock_http_response = {
            "success": True,
            "requested_url": "https://spa.example.com",
            "final_url": "https://spa.example.com",
            "status_code": 200,
            "render_method": "http",
            "content": "You need to enable JavaScript to run this app.",
            "title": "Loading React App...",
            "raw_html": spa_html,
            "content_type": "text/html",
            "headers": {},
        }

        rendered_html = """<!DOCTYPE html>
<html>
<head><title>Hydrated SPA App</title></head>
<body>
    <div id="root">
        <h1>Welcome to Hydrated Single Page App</h1>
        <p>Full content rendered via headless Chromium.</p>
    </div>
</body>
</html>"""

        mock_browser_response = {
            "success": True,
            "requested_url": "https://spa.example.com",
            "final_url": "https://spa.example.com",
            "status_code": 200,
            "render_method": "browser",
            "content": "Welcome to Hydrated Single Page App",
            "title": "Hydrated SPA App",
            "raw_html": rendered_html,
            "content_type": "text/html",
            "headers": {},
        }

        with patch("smartfetch.inspect.validate_public_url", return_value="https://spa.example.com"), \
             patch("smartfetch.inspect.http_fetch", return_value=mock_http_response), \
             patch("smartfetch.inspect.browser_fetch", return_value=mock_browser_response) as mock_b:
            result = inspect_webpage_url("https://spa.example.com", render_mode="auto", no_cache=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["retrieval_tier"], "browser")
        self.assertEqual(result["render_method"], "browser")
        self.assertEqual(result["metadata"]["title"], "Hydrated SPA App")
        self.assertEqual(result["structure"]["h1"], ["Welcome to Hydrated Single Page App"])
        mock_b.assert_called_once()

    def test_http_only_mode_prevents_escalation(self):
        spa_html = '<div id="app"></div><noscript>Enable JS</noscript>'
        mock_http_response = {
            "success": True,
            "requested_url": "https://spa.example.com",
            "final_url": "https://spa.example.com",
            "status_code": 200,
            "render_method": "http",
            "content": "Enable JS",
            "title": "SPA",
            "raw_html": spa_html,
            "content_type": "text/html",
            "headers": {},
        }

        with patch("smartfetch.inspect.validate_public_url", return_value="https://spa.example.com"), \
             patch("smartfetch.inspect.http_fetch", return_value=mock_http_response), \
             patch("smartfetch.inspect.browser_fetch") as mock_b:
            result = inspect_webpage_url("https://spa.example.com", render_mode="http_only", no_cache=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["retrieval_tier"], "http")
        mock_b.assert_not_called()

    def test_ssrf_protection_rejects_loopback(self):
        with self.assertRaises(ValueError):
            inspect_webpage_url("http://127.0.0.1:8080/admin", no_cache=True)


class InspectRestApiTests(unittest.TestCase):
    def test_operational_unpaid_empty_probe_returns_402_challenge(self):
        settings = X402Settings(
            enabled=True,
            pay_to=VALID_PAY_TO,
            price="$0.005",
            network=BASE_MAINNET,
            cdp_api_key_id="test-id",
            cdp_api_key_secret="test-secret",
        )
        with patch.dict("os.environ", {
            "BROWSER_WORKER_URL": "https://159.89.40.174:9443",
            "BROWSER_WORKER_HMAC_KEY": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "BROWSER_TLS_CA_CERT": "dummy_ca_cert",
            "BROWSER_TLS_CLIENT_CERT": "dummy_client_cert",
            "BROWSER_TLS_CLIENT_KEY": "dummy_client_key",
            "X402_ALLOW_MOCK_FACILITATOR": "true",
        }):
            with patch("smartfetch.payments.create_facilitator") as mock_fac:
                mock_client = MagicMock()
                mock_client.get_supported.return_value = SupportedResponse(
                    kinds=[SupportedKind(x402Version=2, scheme="exact", network=BASE_MAINNET)]
                )
                mock_fac.return_value = mock_client
                app = server.create_app(settings)
                with TestClient(app) as client:
                    resp = client.post("/inspect", json={})
                    self.assertEqual(resp.status_code, 402)
                    req_header = resp.headers.get("payment-required")
                    self.assertIsNotNone(req_header)
                    decoded = json.loads(base64.b64decode(req_header).decode("utf-8"))
                    self.assertEqual(decoded["accepts"][0]["amount"], "5000")  # $0.005 USDC
                    self.assertEqual(decoded["accepts"][0]["network"], BASE_MAINNET)
                    self.assertEqual(decoded["accepts"][0]["payTo"], VALID_PAY_TO)

    def test_emergency_fail_closed_mode_returns_503(self):
        settings = X402Settings(
            True,
            VALID_PAY_TO,
            "$0.005",
            BASE_MAINNET,
            "organizations/test/apiKeys/test",
            "never-commit-cdp-secret",
        )
        empty_cfg = RemoteBrowserConfig(worker_url=None, hmac_key=None)

        with patch.object(RemoteBrowserConfig, "from_env", return_value=empty_cfg):
            app = server.create_app(settings)

        with TestClient(app) as client:
            resp = client.post("/inspect", json={"url": "https://example.com"})

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error_code"], "retrieval_unavailable")


class InspectMCPToolTests(unittest.TestCase):
    def test_inspect_tool_in_tools_list_and_schema(self):
        settings = X402Settings(
            False,
            None,
            "$0.005",
            BASE_SEPOLIA,
        )
        mock_fetch = AsyncMock()
        mcp = create_smartfetch_mcp(settings, mock_fetch)
        tools = mcp.fastmcp._tool_manager._tools
        self.assertIn("inspect_webpage", tools)

        tool = tools["inspect_webpage"]
        schema = tool.parameters
        props = schema["properties"]
        self.assertIn("url", props)
        self.assertIn("urls", props)
        self.assertIn("force_browser", props)
        self.assertIn("render_mode", props)


if __name__ == "__main__":
    unittest.main()
