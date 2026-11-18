"""Tests for SmartFetch v1.15.0 opt-in Search, Search & Fetch, B2B Leads, and X Search."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from smartfetch.apify_actor import execute_action
from smartfetch.cache import get_cache_provider
from smartfetch.leads import search_leads_query
from smartfetch.payments import X402Settings
from smartfetch.search_tools import search_and_fetch_query, search_web_query
from smartfetch.server import create_app
from smartfetch.social import search_x_posts_query


_ROOT = Path(__file__).resolve().parent.parent


def _test_payment_settings(*, enabled: bool = True) -> X402Settings:
    return X402Settings(
        enabled=enabled,
        network="eip155:8453",
        pay_to="0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786",
        price="$0.005",
        solana_network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        solana_pay_to="88hE4EB4yZwb4F2arPXcG7efwQyU5FDN26DuVpVKVk2W",
    )


def _fake_remote_cfg():
    return SimpleNamespace(
        is_configured=True,
        worker_url="https://127.0.0.1:9443",
        shared_secret="test-secret",
    )


class TestV115OptInBehavior(unittest.TestCase):
    def setUp(self):
        get_cache_provider().clear()

    def test_default_without_keys_preserves_5_routes_8_tools_and_503_on_optin(self):
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SERPER_API_KEY", "EXA_API_KEY", "TWITTERAPI_KEY")
        }
        with patch.dict(os.environ, clean_env, clear=True):
            with patch("smartfetch.remote_browser.RemoteBrowserConfig.from_env", return_value=_fake_remote_cfg()):
                app = create_app(payment_settings=_test_payment_settings(enabled=False))
                with TestClient(app) as client:

                    for path in ("/search", "/search-and-fetch", "/leads", "/x-search"):
                        resp = client.post(path, json={"query": "test"})
                        self.assertEqual(resp.status_code, 503, f"{path} should return 503 when unconfigured")
                        self.assertEqual(resp.json()["error_code"], "provider_unavailable")

                    openapi = client.get("/openapi.json").json()
                    for p in ("/search", "/search-and-fetch", "/leads", "/x-search"):
                        self.assertNotIn(p, openapi["paths"])

                    meta = client.get("/meta").json()
                    self.assertEqual(len(meta["mcp"]["tools"]), 8)
                    self.assertFalse(meta["enabled_features"]["search"])
                    self.assertFalse(meta["enabled_features"]["leads"])
                    self.assertFalse(meta["enabled_features"]["x_search"])

                    mcp_resp = client.post(
                        "/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                        headers={"Accept": "application/json, text/event-stream"},
                    )
                    self.assertEqual(mcp_resp.status_code, 200)
                    tool_names = [t["name"] for t in mcp_resp.json()["result"]["tools"]]
                    self.assertEqual(len(tool_names), 8)

    def test_search_auto_prefers_serper_over_exa_and_caches(self):
        serper_payload = {
            "organic": [
                {"title": "Serper Hit", "link": "https://example.com/a", "snippet": "Hello world"}
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = serper_payload

        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test", "EXA_API_KEY": "exa-test"}):
            with patch("smartfetch.search_tools.requests.Session.post", return_value=mock_resp) as post_mock:
                res1 = search_web_query("x402 protocol", provider="auto", num_results=5)
                self.assertTrue(res1["success"])
                self.assertEqual(res1["provider_used"], "serper")
                self.assertFalse(res1["cached"])
                self.assertEqual(post_mock.call_count, 1)
                self.assertIn("google.serper.dev", post_mock.call_args.args[0])

                # Second call hits cache
                res2 = search_web_query("x402 protocol", provider="auto", num_results=5)
                self.assertTrue(res2["cached"])
                self.assertEqual(post_mock.call_count, 1)

    def test_exa_explicit_uses_highlights_only(self):
        exa_payload = {
            "results": [
                {"title": "Exa Hit", "url": "https://example.org", "highlights": ["Key highlight"]}
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = exa_payload

        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test", "EXA_API_KEY": "exa-test"}):
            with patch("smartfetch.search_tools.requests.Session.post", return_value=mock_resp) as post_mock:
                res = search_web_query("agentic web", provider="exa", num_results=3)
                self.assertEqual(res["provider_used"], "exa")
                sent_json = post_mock.call_args.kwargs["json"]
                self.assertEqual(sent_json["contents"], {"highlights": {"maxCharacters": 500}})

    def test_search_and_fetch_returns_502_when_all_pages_fail(self):
        serper_resp = MagicMock()
        serper_resp.status_code = 200
        serper_resp.json.return_value = {
            "organic": [{"title": "Example", "link": "https://example.com/p1", "snippet": "s"}]
        }

        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test"}):
            with patch("smartfetch.remote_browser.RemoteBrowserConfig.from_env", return_value=_fake_remote_cfg()):
                with patch("smartfetch.search_tools.requests.Session.post", return_value=serper_resp):
                    with patch("smartfetch.search_tools.batch_fetch_urls", return_value={"succeeded_count": 0, "failed_count": 1, "results": []}):
                        app = create_app(payment_settings=_test_payment_settings(enabled=False))
                        client = TestClient(app)
                        resp = client.post("/search-and-fetch", json={"query": "x402"})
                        self.assertEqual(resp.status_code, 502)

    def test_leads_merges_maps_and_resilient_deep_inspect(self):
        maps_resp = MagicMock()
        maps_resp.status_code = 200
        maps_resp.json.return_value = {
            "places": [
                {"title": "Clinic A", "website": "https://example.com", "address": "1 Main St", "phoneNumber": "555-0100", "rating": 4.9, "ratingCount": 42},
                {"title": "Clinic B", "website": "https://blocked.example.org", "address": "2 Oak St"},
            ]
        }

        def fake_inspect(url, **kwargs):
            if "blocked" in url:
                raise RuntimeError("Blocked")
            return {
                "contacts": {"emails": ["hello@example.com"], "phones": ["555-0100"], "contact_url": "https://example.com/contact", "pricing_url": None},
                "social_links": {"linkedin": "https://linkedin.com/company/clinica"},
                "tech_stack": ["Next.js"],
                "domain_intel": {"has_mx": True, "has_spf": True, "has_dmarc": False, "domain_age_days": 1200},
                "meta": {"title": "Clinic A Home", "description": "Dental care"},
            }

        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test"}):
            with patch("smartfetch.leads.requests.Session.post", return_value=maps_resp):
                with patch("smartfetch.leads.inspect_webpage_url", side_effect=fake_inspect):
                    res = search_leads_query("dental clinics austin", source="maps", max_leads=5)
                    self.assertTrue(res["success"])
                    self.assertEqual(res["total_leads"], 2)
                    self.assertEqual(res["inspected_count"], 1)
                    self.assertEqual(res["leads"][0]["emails"], ["hello@example.com"])
                    self.assertTrue(res["leads"][0]["inspected"])
                    self.assertFalse(res["leads"][1]["inspected"])

    def test_mcp_tools_call_enforces_per_tool_price_and_how_to_pay_and_llms_txt(self):
        expected_amounts = {
            "fetch_webpage": "5000",
            "screenshot_webpage": "10000",
            "batch_fetch_webpages": "15000",
            "search_web": "10000",
            "search_and_fetch": "20000",
            "search_leads": "30000",
            "search_x_posts": "8000",
        }
        tool_args = {
            "fetch_webpage": {"url": "https://example.com"},
            "screenshot_webpage": {"url": "https://example.com"},
            "batch_fetch_webpages": {"urls": ["https://example.com"]},
            "search_web": {"query": "x402"},
            "search_and_fetch": {"query": "x402"},
            "search_leads": {"query": "dentists austin"},
            "search_x_posts": {"query": "x402"},
        }
        with patch.dict(os.environ, {"SERPER_API_KEY": "s", "EXA_API_KEY": "e", "TWITTERAPI_KEY": "t"}):
            with patch("smartfetch.remote_browser.RemoteBrowserConfig.from_env", return_value=_fake_remote_cfg()):
                app = create_app(payment_settings=_test_payment_settings(enabled=True))
                with TestClient(app) as client:
                    # 1. REST 402 includes how_to_pay alongside PAYMENT-REQUIRED
                    r402 = client.post("/leads", json={"query": "dentists austin"})
                    self.assertEqual(r402.status_code, 402)
                    self.assertIn("payment-required", {k.lower(): v for k, v in r402.headers.items()})
                    body402 = r402.json()
                    self.assertIn("how_to_pay", body402)
                    self.assertIn("option_1_x402", body402["how_to_pay"])
                    self.assertIn("option_2_apify_standby_mcp", body402["how_to_pay"])
                    self.assertIn("option_3_browser_playground", body402["how_to_pay"])

                    # 2. MCP tools/call requires exact per-tool price and includes how_to_pay
                    for tool_name, expected_amt in expected_amounts.items():
                        mcp_resp = client.post(
                            "/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": 10,
                                "method": "tools/call",
                                "params": {"name": tool_name, "arguments": tool_args[tool_name]},
                            },
                            headers={"Accept": "application/json, text/event-stream"},
                        )
                        self.assertEqual(mcp_resp.status_code, 200)
                        res = mcp_resp.json()["result"]
                        self.assertTrue(res["isError"], f"{tool_name} must not execute unpaid")
                        sc = res["structuredContent"]
                        self.assertEqual(sc["x402Version"], 2)
                        self.assertIn("how_to_pay", sc)
                        amounts = {a["amount"] for a in sc["accepts"]}
                        self.assertEqual(amounts, {expected_amt})

                    # 3. GET /llms.txt documents all 9 routes & How to Pay
                    llms = client.get("/llms.txt").text
                    for route in (
                        "/fetch",
                        "/screenshot",
                        "/inspect",
                        "/batch-fetch",
                        "/map",
                        "/search",
                        "/search-and-fetch",
                        "/leads",
                        "/x-search",
                    ):
                        self.assertIn(route, llms)
                    self.assertIn("How to Pay", llms)
                    self.assertIn("smartfetch-dev--smartfetch.apify.actor/mcp", llms)
                    self.assertEqual(res["leads"][1]["business_name"], "Clinic B")

    def test_x_search_hard_caps_20_and_preserves_cursor(self):
        tweets_raw = [
            {
                "id": str(i),
                "url": f"https://x.com/u/status/{i}",
                "text": f"Post {i}",
                "createdAt": "2026-11-18T00:00:00Z",
                "author": {"userName": f"user{i}", "name": f"User {i}", "followers": 100},
                "likeCount": 5,
                "retweetCount": 1,
                "replyCount": 0,
                "viewCount": 50,
            }
            for i in range(25)
        ]
        payload_bytes = json.dumps({
            "tweets": tweets_raw,
            "next_cursor": "cursor_abc_123",
            "has_next_page": True,
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_content.return_value = [payload_bytes]

        with patch.dict(os.environ, {"TWITTERAPI_KEY": "tw-test"}):
            with patch("smartfetch.social.requests.Session.get", return_value=mock_resp):
                res = search_x_posts_query("x402", max_tweets=99)
                self.assertTrue(res["success"])
                self.assertEqual(res["count"], 20)
                self.assertEqual(res["next_cursor"], "cursor_abc_123")
                self.assertTrue(res["has_next_page"])

    def test_enabled_routes_expose_12_mcp_tools_openapi_and_402_challenges(self):
        with patch.dict(os.environ, {"SERPER_API_KEY": "s", "EXA_API_KEY": "e", "TWITTERAPI_KEY": "t"}):
            with patch("smartfetch.remote_browser.RemoteBrowserConfig.from_env", return_value=_fake_remote_cfg()):
                app = create_app(payment_settings=_test_payment_settings(enabled=True))
                with TestClient(app) as client:

                    openapi = client.get("/openapi.json").json()
                    for p in ("/search", "/search-and-fetch", "/leads", "/x-search"):
                        self.assertIn(p, openapi["paths"])
                        self.assertEqual(openapi["paths"][p]["post"]["security"], [])

                    mcp_resp = client.post(
                        "/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                        headers={"Accept": "application/json, text/event-stream"},
                    )
                    self.assertEqual(mcp_resp.status_code, 200)
                    tool_names = [t["name"] for t in mcp_resp.json()["result"]["tools"]]
                    self.assertEqual(len(tool_names), 12)
                    for name in ("search_web", "search_and_fetch", "search_leads", "search_x_posts"):
                        self.assertIn(name, tool_names)

                    for path in ("/search", "/search-and-fetch", "/leads", "/x-search"):
                        r402 = client.post(path, json={"query": "x402"})
                        self.assertEqual(r402.status_code, 402, f"{path} should return 402 when enabled and unpaid")

    def test_apify_search_leads_dataset_schema_and_free_cap(self):
        import asyncio

        schema = json.loads((_ROOT / ".actor" / "dataset_schema.json").read_text(encoding="utf-8"))
        allowed_props = set(schema["fields"]["properties"].keys())

        fake_actor = MagicMock()
        pushed = []

        async def _push(row):
            pushed.append(row)

        async def _charge(**kwargs):
            return None

        fake_actor.push_data = MagicMock(side_effect=_push)
        fake_actor.charge = MagicMock(side_effect=_charge)

        mock_leads_res = {
            "success": True,
            "query": "coffee austin",
            "source": "maps",
            "total_leads": 2,
            "inspected_count": 2,
            "elapsed_ms": 150,
            "leads": [
                {
                    "business_name": "Bean Co",
                    "category": "Cafe",
                    "rating": 4.8,
                    "reviews_count": 100,
                    "address": "100 Congress",
                    "maps_phone": "555-1234",
                    "website": "https://beanco.example.com",
                    "inspected": True,
                    "emails": ["hi@beanco.example.com"],
                    "phones": ["555-1234"],
                    "social_links": {"linkedin": "https://linkedin.com/company/beanco"},
                    "tech_stack": ["Shopify"],
                    "contact_url": "https://beanco.example.com/contact",
                    "pricing_url": None,
                    "has_mx": True,
                    "has_spf": True,
                    "has_dmarc": True,
                    "domain_age_days": 500,
                    "title": "Bean Co",
                    "description": "Coffee",
                }
            ],
        }

        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test", "APIFY_USER_IS_PAYING": "0"}):
            with patch("smartfetch.leads.search_leads_query", return_value=mock_leads_res) as leads_mock:
                res = asyncio.run(
                    execute_action(
                        {"action": "search_leads", "query": "coffee austin", "max_leads": 25},
                        actor=fake_actor,
                        environ=os.environ,
                    )
                )
                self.assertTrue(res["success"])
                self.assertEqual(res["max_leads_allowed"], 5)
                self.assertEqual(leads_mock.call_args.kwargs["_max_allowed"], 5)
                self.assertEqual(len(pushed), 1)
                self.assertTrue(set(pushed[0].keys()).issubset(allowed_props))
                fake_actor.charge.assert_called_once_with(event_name="site-inspected", count=1)

    def test_search_places_extracts_website_and_falls_back_to_maps(self):
        places_with_website = MagicMock()
        places_with_website.status_code = 200
        places_with_website.json.return_value = {
            "places": [
                {"title": "Smile Austin", "website": "https://smileaustin.example.com", "phoneNumber": "512-555-0101", "address": "101 Congress Ave", "rating": 4.9, "ratingCount": 88, "category": "Dentist"}
            ]
        }
        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test"}):
            with patch("smartfetch.search_tools.requests.Session.post", return_value=places_with_website) as post_mock:
                res = search_web_query("dentists austin", search_type="places", num_results=5, no_cache=True)
                self.assertEqual(res["results"][0]["url"], "https://smileaustin.example.com")
                self.assertEqual(res["results"][0]["phone"], "512-555-0101")
                self.assertEqual(post_mock.call_count, 1)

        places_no_website = MagicMock()
        places_no_website.status_code = 200
        places_no_website.json.return_value = {"places": [{"title": "Pin Only", "address": "200 Main"}]}
        maps_with_website = MagicMock()
        maps_with_website.status_code = 200
        maps_with_website.json.return_value = {
            "places": [{"title": "Maps Dental", "website": "https://mapsdental.example.com", "phone": "512-555-0202", "reviews": 19}]
        }
        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test"}):
            with patch("smartfetch.search_tools.requests.Session.post", side_effect=[places_no_website, maps_with_website]) as post_mock:
                res = search_web_query("dentists austin maps fallback", search_type="places", num_results=5, no_cache=True)
                self.assertEqual(post_mock.call_count, 2)
                self.assertIn("/maps", post_mock.call_args_list[1].args[0])
                self.assertEqual(res["results"][0]["url"], "https://mapsdental.example.com")
                self.assertEqual(res["results"][0]["reviews_count"], 19)

    def test_leads_places_falls_back_to_web_and_preserves_pin_without_website(self):
        empty_resp = MagicMock()
        empty_resp.status_code = 200
        empty_resp.json.return_value = {"places": []}
        web_resp = MagicMock()
        web_resp.status_code = 200
        web_resp.json.return_value = {
            "organic": [{"title": "SaaS Co", "link": "https://example.com", "snippet": "B2B SaaS"}]
        }

        def fake_inspect(url, **kwargs):
            return {
                "contacts": {"emails": ["sales@saas.example.com"], "phones": ["512-555-0303"], "contact_url": None, "pricing_url": None},
                "social_links": {},
                "tech_stack": ["Stripe"],
                "domain_intel": {"has_mx": True, "has_spf": True, "has_dmarc": True, "domain_age_days": 900},
                "meta": {"title": "SaaS Co", "description": "B2B"},
            }

        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test"}):
            with patch("smartfetch.leads.requests.Session.post", side_effect=[empty_resp, empty_resp, web_resp]) as post_mock:
                with patch("smartfetch.leads.inspect_webpage_url", side_effect=fake_inspect):
                    res = search_leads_query("SaaS companies", source="places", max_leads=3, no_cache=True)
                    self.assertEqual(post_mock.call_count, 3)
                    self.assertTrue(res["success"])
                    self.assertEqual(res["leads"][0]["emails"], ["sales@saas.example.com"])

        # Preserves Maps pin without website alongside inspected lead with website
        mixed_places = MagicMock()
        mixed_places.status_code = 200
        mixed_places.json.return_value = {
            "places": [
                {"title": "With Site", "website": "https://example.com", "phoneNumber": "512-555-0001", "address": "10 A St", "rating": 4.8},
                {"title": "No Site Pin", "phoneNumber": "512-555-0002", "address": "20 B St", "rating": 4.6},
            ]
        }
        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-test"}):
            with patch("smartfetch.leads.requests.Session.post", return_value=mixed_places):
                with patch("smartfetch.leads.inspect_webpage_url", side_effect=fake_inspect):
                    res = search_leads_query("dentists austin mixed", source="places", max_leads=5, no_cache=True)
                    self.assertEqual(res["total_leads"], 2)
                    self.assertEqual(res["inspected_count"], 1)
                    self.assertEqual(res["leads"][1]["business_name"], "No Site Pin")
                    self.assertEqual(res["leads"][1]["phones"], ["512-555-0002"])
                    self.assertFalse(res["leads"][1]["inspected"])

    def test_mcp_tools_call_enforces_per_tool_price_and_how_to_pay_and_llms_txt(self):
        expected_amounts = {
            "fetch_webpage": "5000",
            "screenshot_webpage": "10000",
            "batch_fetch_webpages": "15000",
            "search_web": "10000",
            "search_and_fetch": "20000",
            "search_leads": "30000",
            "search_x_posts": "8000",
        }
        tool_args = {
            "fetch_webpage": {"url": "https://example.com"},
            "screenshot_webpage": {"url": "https://example.com"},
            "batch_fetch_webpages": {"urls": ["https://example.com"]},
            "search_web": {"query": "x402"},
            "search_and_fetch": {"query": "x402"},
            "search_leads": {"query": "dentists austin"},
            "search_x_posts": {"query": "x402"},
        }
        with patch.dict(os.environ, {"SERPER_API_KEY": "s", "EXA_API_KEY": "e", "TWITTERAPI_KEY": "t"}):
            with patch("smartfetch.remote_browser.RemoteBrowserConfig.from_env", return_value=_fake_remote_cfg()):
                app = create_app(payment_settings=_test_payment_settings(enabled=True))
                with TestClient(app) as client:
                    # 1. REST 402 includes how_to_pay alongside PAYMENT-REQUIRED
                    r402 = client.post("/leads", json={"query": "dentists austin"})
                    self.assertEqual(r402.status_code, 402)
                    self.assertIn("payment-required", {k.lower(): v for k, v in r402.headers.items()})
                    body402 = r402.json()
                    self.assertIn("how_to_pay", body402)
                    self.assertIn("option_1_x402", body402["how_to_pay"])
                    self.assertIn("option_2_apify_standby_mcp", body402["how_to_pay"])
                    self.assertIn("option_3_browser_playground", body402["how_to_pay"])

                    # 2. MCP tools/call requires exact per-tool price and includes how_to_pay
                    for tool_name, expected_amt in expected_amounts.items():
                        mcp_resp = client.post(
                            "/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": 10,
                                "method": "tools/call",
                                "params": {"name": tool_name, "arguments": tool_args[tool_name]},
                            },
                            headers={"Accept": "application/json, text/event-stream"},
                        )
                        self.assertEqual(mcp_resp.status_code, 200)
                        res = mcp_resp.json()["result"]
                        self.assertTrue(res["isError"], f"{tool_name} must not execute unpaid")
                        sc = res["structuredContent"]
                        self.assertEqual(sc["x402Version"], 2)
                        self.assertIn("how_to_pay", sc)
                        amounts = {a["amount"] for a in sc["accepts"]}
                        self.assertEqual(amounts, {expected_amt})

                    # 3. GET /llms.txt documents all 9 routes & How to Pay
                    llms = client.get("/llms.txt").text
                    for route in (
                        "/fetch",
                        "/screenshot",
                        "/inspect",
                        "/batch-fetch",
                        "/map",
                        "/search",
                        "/search-and-fetch",
                        "/leads",
                        "/x-search",
                    ):
                        self.assertIn(route, llms)
                    self.assertIn("How to Pay", llms)
                    self.assertIn("smartfetch-dev--smartfetch.apify.actor/mcp", llms)

    def test_get_support_on_all_9_paid_routes_and_openapi_unchanged(self):
        import base64

        routes = (
            "/fetch",
            "/screenshot",
            "/inspect",
            "/batch-fetch",
            "/map",
            "/search",
            "/search-and-fetch",
            "/leads",
            "/x-search",
        )
        with patch.dict(os.environ, {"SERPER_API_KEY": "s", "EXA_API_KEY": "e", "TWITTERAPI_KEY": "t"}):
            with patch("smartfetch.remote_browser.RemoteBrowserConfig.from_env", return_value=_fake_remote_cfg()):
                app = create_app(payment_settings=_test_payment_settings(enabled=True))
                with TestClient(app) as client:
                    openapi = client.get("/openapi.json").json()
                    for path in routes:
                        self.assertEqual(list(openapi["paths"][path].keys()), ["post"])
                        r_get = client.get(path)
                        self.assertEqual(r_get.status_code, 402, f"GET {path} should return 402 when unpaid")
                        hdrs = {k.lower(): v for k, v in r_get.headers.items()}
                        self.assertIn("payment-required", hdrs)
                        decoded = json.loads(base64.b64decode(hdrs["payment-required"]).decode("utf-8"))
                        networks = {a["network"] for a in decoded["accepts"]}
                        self.assertIn("eip155:8453", networks)
                        self.assertIn("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", networks)
                        self.assertIn("how_to_pay", r_get.json())

                # Paid/authorized GET with query params and bare verifier defaults (enabled=False simulates post-payment)
                app_free = create_app(payment_settings=_test_payment_settings(enabled=False))
                with TestClient(app_free) as client_free:
                    with patch("smartfetch.server.smart_fetch", return_value={"success": True, "title": "Example", "content": "ok", "markdown": "ok", "render_method": "http", "elapsed_ms": 5}):
                        r_bare = client_free.get("/fetch")
                        self.assertEqual(r_bare.status_code, 200)
                        r_q = client_free.get("/fetch?url=https://example.com&format=markdown")
                        self.assertEqual(r_q.status_code, 200)
                    with patch("smartfetch.server.search_web_query", return_value={"success": True, "query": "x402", "provider_used": "serper", "search_type": "web", "num_results": 1, "results": [], "cached": False, "elapsed_ms": 5, "service_version": "1.15.0"}) as search_mock:
                        r_s = client_free.get("/search?q=ai+agents&type=web&max_results=3")
                        self.assertEqual(r_s.status_code, 200)
                        self.assertEqual(search_mock.call_args.args[0], "ai agents")
                        r_s_bare = client_free.get("/search")
                        self.assertEqual(r_s_bare.status_code, 200)
                        self.assertEqual(search_mock.call_args.args[0], "x402")
                    with patch("smartfetch.server.search_leads_query", return_value={"success": True, "query": "software companies in Austin TX", "source": "maps", "total_leads": 0, "inspected_count": 0, "leads": [], "cached": False, "elapsed_ms": 5, "service_version": "1.15.0"}) as leads_mock:
                        r_l_bare = client_free.get("/leads")
                        self.assertEqual(r_l_bare.status_code, 200)
                        self.assertEqual(leads_mock.call_args.args[0], "software companies in Austin TX")


if __name__ == "__main__":
    unittest.main()
