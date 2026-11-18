"""Unit tests for Upgrade C: Deep /inspect enrichment, DoH/RDAP domain_intel, RSS feeds, and Apify Dataset schema."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import jsonschema

from smartfetch import apify_actor
from smartfetch.cache import build_inspect_cache_key, get_result_cache
from smartfetch.inspect import collect_domain_intel, inspect_webpage_url


REPO_ROOT = Path(__file__).resolve().parent.parent


class DeepInspectTests(unittest.TestCase):
    def setUp(self):
        get_result_cache().clear()

    @patch("smartfetch.inspect.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.inspect.collect_domain_intel")
    @patch("smartfetch.inspect.http_fetch")
    def test_deep_inspect_merges_contact_and_pricing_and_extracts_rss(
        self,
        mock_http_fetch,
        mock_intel,
        _mock_validate,
    ):
        mock_intel.return_value = {
            "domain": "acme.com",
            "has_mx": True,
            "mx_hosts": ["aspmx.l.google.com"],
            "has_spf": True,
            "has_dmarc": True,
            "registrar": "Cloudflare, Inc.",
            "created_at": "2020-01-01T00:00:00Z",
            "domain_age_days": 2000,
        }

        def fake_fetch(url: str):
            if url == "https://acme.com/":
                return {
                    "status_code": 200,
                    "final_url": "https://acme.com/",
                    "headers": {"strict-transport-security": "max-age=31536000"},
                    "html": (
                        "<html><head>"
                        "<title>Acme Corp</title>"
                        '<link rel="alternate" type="application/rss+xml" href="/feed.xml" />'
                        "</head><body>"
                        '<a href="/contact">Contact Us</a>'
                        '<a href="/pricing">Pricing</a>'
                        '<a href="https://github.com/acme">GitHub</a>'
                        "</body></html>"
                    ),
                }
            if url == "https://acme.com/contact":
                return {
                    "status_code": 200,
                    "final_url": "https://acme.com/contact",
                    "headers": {},
                    "html": (
                        "<html><body>"
                        '<a href="mailto:sales@acme.com">Email Sales</a>'
                        '<a href="tel:+14155550100">Call Us</a>'
                        '<a href="https://linkedin.com/company/acme">LinkedIn</a>'
                        "</body></html>"
                    ),
                }
            if url == "https://acme.com/pricing":
                return {
                    "status_code": 200,
                    "final_url": "https://acme.com/pricing",
                    "headers": {},
                    "html": "<html><body><p>Enterprise: enterprise@acme.com</p></body></html>",
                }
            raise RuntimeError(f"Unexpected URL: {url}")

        mock_http_fetch.side_effect = fake_fetch

        res = inspect_webpage_url("https://acme.com/", no_cache=True, deep=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["contacts"]["emails"], ["sales@acme.com", "enterprise@acme.com"])
        self.assertEqual(res["contacts"]["phones"], ["+14155550100"])
        self.assertEqual(res["contacts"]["social_links"]["github"], "https://github.com/acme")
        self.assertEqual(res["contacts"]["social_links"]["linkedin"], "https://linkedin.com/company/acme")
        self.assertEqual(res["contacts"]["contact_url"], "https://acme.com/contact")
        self.assertEqual(res["contacts"]["pricing_url"], "https://acme.com/pricing")
        self.assertEqual(
            res["contacts"]["pages_checked"],
            ["https://acme.com/", "https://acme.com/contact", "https://acme.com/pricing"],
        )
        self.assertEqual(res["structure"]["rss_feeds"], ["https://acme.com/feed.xml"])
        self.assertEqual(res["domain_intel"]["registrar"], "Cloudflare, Inc.")
        self.assertEqual(res["domain_intel"]["domain_age_days"], 2000)

    @patch("smartfetch.inspect.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.inspect.http_fetch")
    def test_deep_false_skips_sub_fetches_and_separates_cache_key(self, mock_http_fetch, _mock_validate):
        mock_http_fetch.return_value = {
            "status_code": 200,
            "final_url": "https://acme.com/",
            "headers": {},
            "html": '<html><body><a href="/contact">Contact</a><a href="/pricing">Pricing</a></body></html>',
        }

        res = inspect_webpage_url("https://acme.com/", no_cache=True, deep=False)
        self.assertTrue(res["success"])
        self.assertEqual(mock_http_fetch.call_count, 1)
        self.assertEqual(res["contacts"]["pages_checked"], ["https://acme.com/"])

        k_default = build_inspect_cache_key("https://acme.com/")
        k_deep = build_inspect_cache_key("https://acme.com/", deep=True)
        k_nodeep = build_inspect_cache_key("https://acme.com/", deep=False)
        self.assertEqual(k_default, k_deep)
        self.assertNotEqual(k_default, k_nodeep)
        self.assertTrue(k_nodeep.endswith(":nodeep"))


class DomainIntelTests(unittest.TestCase):
    def test_collect_domain_intel_parses_doh_mx_spf_dmarc_and_rdap(self):
        def fake_get(url, params=None, **_kw):
            resp = MagicMock()
            resp.status_code = 200
            if "dns.google" in url:
                qtype = params.get("type")
                name = params.get("name")
                if qtype == "MX":
                    resp.json.return_value = {"Answer": [{"type": 15, "data": "10 mail.acme.com."}]}
                elif qtype == "TXT" and name == "acme.com":
                    resp.json.return_value = {"Answer": [{"type": 16, "data": '"v=spf1 include:_spf.google.com ~all"'}]}
                elif qtype == "TXT" and name == "_dmarc.acme.com":
                    resp.json.return_value = {"Answer": [{"type": 16, "data": '"v=DMARC1; p=reject;"'}]}
                else:
                    resp.json.return_value = {}
                return resp
            if "rdap.org" in url:
                resp.json.return_value = {
                    "events": [{"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"}],
                    "entities": [
                        {
                            "roles": ["registrar"],
                            "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar LLC"]]],
                        }
                    ],
                }
                return resp
            return resp

        with patch("smartfetch.inspect.requests.get", side_effect=fake_get):
            intel = collect_domain_intel("https://docs.acme.com/guide", timeout=1.5)

        self.assertEqual(intel["domain"], "acme.com")
        self.assertTrue(intel["has_mx"])
        self.assertEqual(intel["mx_hosts"], ["mail.acme.com"])
        self.assertTrue(intel["has_spf"])
        self.assertTrue(intel["has_dmarc"])
        self.assertEqual(intel["registrar"], "Example Registrar LLC")
        self.assertGreater(intel["domain_age_days"], 1000)

    def test_collect_domain_intel_and_subpages_never_fail_on_timeouts(self):
        with patch("smartfetch.inspect.requests.get", side_effect=TimeoutError("Timed out")):
            intel = collect_domain_intel("https://acme.com/", timeout=0.1)

        self.assertEqual(intel["domain"], "acme.com")
        self.assertFalse(intel["has_mx"])
        self.assertFalse(intel["has_spf"])
        self.assertFalse(intel["has_dmarc"])
        self.assertIsNone(intel["domain_age_days"])


class ApifyDeepBulkInspectSchemaTests(unittest.TestCase):
    def test_bulk_inspect_flattened_row_with_domain_intel_passes_dataset_schema(self):
        from tests.test_v114_apify_actor import FakeApifyActor

        fake_actor = FakeApifyActor({"action": "bulk_inspect", "urls": ["acme.com"]})
        with patch("smartfetch.apify_actor.inspect_webpage_url") as mock_inspect:
            mock_inspect.return_value = {
                "success": True,
                "requested_url": "https://acme.com",
                "final_url": "https://acme.com/",
                "status_code": 200,
                "render_method": "http",
                "elapsed_ms": 120,
                "metadata": {"title": "Acme", "description": "Corp"},
                "contacts": {
                    "emails": ["hi@acme.com"],
                    "phones": [],
                    "social_links": {},
                    "contact_url": "https://acme.com/contact",
                    "pricing_url": "https://acme.com/pricing",
                },
                "tech_stack": ["Next.js"],
                "security_headers": {"https": True},
                "domain_intel": {
                    "has_mx": True,
                    "has_spf": True,
                    "has_dmarc": False,
                    "domain_age_days": 1500,
                },
            }
            res = asyncio.run(apify_actor.run_standard_mode(actor_cls=fake_actor))

        self.assertTrue(res["success"])
        row = fake_actor.pushed_items[0]
        self.assertEqual(row["contact_url"], "https://acme.com/contact")
        self.assertEqual(row["pricing_url"], "https://acme.com/pricing")
        self.assertTrue(row["has_mx"])
        self.assertTrue(row["has_spf"])
        self.assertFalse(row["has_dmarc"])
        self.assertEqual(row["domain_age_days"], 1500)

        dataset_schema = json.loads((REPO_ROOT / ".actor" / "dataset_schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(instance=row, schema=dataset_schema["fields"])


if __name__ == "__main__":
    unittest.main()
