"""Unit and integration tests for Step 2: Apify Store Actor, Standby MCP, local screenshot fallback, and 402index verification."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from smartfetch import apify_actor, server
from smartfetch.browser_fetch import browser_screenshot
from smartfetch.payments import BASE_SEPOLIA, X402Settings


REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeApifyActor:
    """Mock Apify Actor class supporting async context manager, get_input, push_data, set_value, and charge."""

    def __init__(self, input_data: dict | None = None):
        self.input_data = input_data or {}
        self.pushed_items: list[dict] = []
        self.kv_store: dict[str, tuple[bytes, str | None]] = {}
        self.charges: list[tuple[str, int]] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False

    async def get_input(self) -> dict:
        return dict(self.input_data)

    async def push_data(self, item: dict) -> None:
        self.pushed_items.append(dict(item))

    async def set_value(self, key: str, value: bytes, content_type: str | None = None) -> None:
        self.kv_store[key] = (value, content_type)

    async def charge(self, event_name: str, count: int = 1) -> None:
        self.charges.append((event_name, count))


class Index402VerificationRouteTests(unittest.TestCase):
    def test_well_known_402index_verify_txt_returns_hash(self):
        app = server.create_app(X402Settings(False, None, "$0.005", BASE_SEPOLIA))
        with TestClient(app) as client:
            res = client.get("/.well-known/402index-verify.txt")
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/plain", res.headers.get("content-type", ""))
            self.assertEqual(
                res.text.strip(),
                "7544f08648ac863080cbd821e8cb34add313ecc58d238d7217e498fd08a80e02",
            )


class LocalBrowserScreenshotFallbackTests(unittest.TestCase):
    def test_fail_closed_when_browser_worker_required_true(self):
        with patch.dict("os.environ", {"BROWSER_WORKER_URL": "", "BROWSER_WORKER_REQUIRED": "true"}):
            with self.assertRaises(RuntimeError) as ctx:
                browser_screenshot("https://example.com", no_cache=True)
            self.assertIn("VPS browser worker is required", str(ctx.exception))

    def test_local_screenshot_rejects_ssrf_url_before_launching_chromium(self):
        with patch.dict("os.environ", {"BROWSER_WORKER_URL": "", "BROWSER_WORKER_REQUIRED": "false"}):
            with patch("smartfetch.browser_pool.WarmChromiumInstance") as mock_inst_cls:
                with self.assertRaises(ValueError):
                    browser_screenshot("http://127.0.0.1:8080/admin", no_cache=True)
                mock_inst_cls.assert_not_called()

    @patch("smartfetch.browser_fetch._chromium_path", return_value="/usr/bin/chromium")
    @patch("smartfetch.browser_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.browser_pool.WarmChromiumInstance")
    def test_local_screenshot_launches_warm_instance_and_always_closes(
        self,
        mock_inst_cls,
        mock_validate,
        _mock_chrome_path,
    ):
        mock_inst = MagicMock()
        fake_b64 = base64.b64encode(b"FAKE_JPEG_BYTES").decode("ascii")
        mock_inst.screenshot.return_value = {
            "success": True,
            "requested_url": "https://example.com",
            "final_url": "https://example.com/",
            "status_code": 200,
            "render_method": "browser",
            "elapsed_ms": 120,
            "title": "Example Domain",
            "image_base64": fake_b64,
            "mime_type": "image/jpeg",
            "image_format": "jpeg",
            "viewport": "desktop",
            "full_page": False,
            "width": 1280,
            "height": 800,
            "byte_size": 15,
        }
        mock_inst_cls.return_value = mock_inst

        with patch.dict("os.environ", {"BROWSER_WORKER_URL": "", "BROWSER_WORKER_REQUIRED": "false"}):
            res = browser_screenshot("https://example.com", viewport="desktop", no_cache=True)

        self.assertTrue(res["success"])
        self.assertEqual(res["image_base64"], fake_b64)
        self.assertFalse(res["cache_hit"])
        mock_inst.start.assert_called_once()
        mock_inst.screenshot.assert_called_once()
        mock_inst.close.assert_called_once()
        self.assertEqual(mock_validate.call_count, 2)

    @patch("smartfetch.browser_fetch._chromium_path", return_value="/usr/bin/chromium")
    @patch("smartfetch.browser_pool.WarmChromiumInstance")
    def test_local_screenshot_validates_final_url_and_closes_on_redirect_ssrf(
        self,
        mock_inst_cls,
        _mock_chrome_path,
    ):
        mock_inst = MagicMock()
        mock_inst.screenshot.return_value = {
            "success": True,
            "requested_url": "https://example.com",
            "final_url": "http://169.254.169.254/latest/meta-data/",
            "status_code": 200,
            "image_base64": base64.b64encode(b"123").decode("ascii"),
        }
        mock_inst_cls.return_value = mock_inst

        def fake_validate(u: str) -> str:
            if "169.254" in u:
                raise ValueError("Private/local/network-metadata target blocked")
            return u

        with patch.dict("os.environ", {"BROWSER_WORKER_URL": "", "BROWSER_WORKER_REQUIRED": "false"}):
            with patch("smartfetch.browser_fetch.validate_public_url", side_effect=fake_validate):
                with self.assertRaises(ValueError):
                    browser_screenshot("https://example.com", no_cache=True)

        mock_inst.close.assert_called_once()


class ApifyPackagingAndSchemaTests(unittest.TestCase):
    def test_actor_json_and_ppe_config_files_are_valid(self):
        actor_json = json.loads((REPO_ROOT / ".actor" / "actor.json").read_text(encoding="utf-8"))
        self.assertEqual(actor_json["actorSpecification"], 1)
        self.assertEqual(actor_json["name"], "smartfetch")
        self.assertTrue(actor_json["usesStandbyMode"])
        self.assertEqual(actor_json["webServerMcpPath"], "/mcp")
        self.assertEqual(actor_json["output"], "./output_schema.json")
        self.assertEqual(actor_json["dockerfile"], "../deploy/apify/Dockerfile")

        output_schema = json.loads((REPO_ROOT / ".actor" / "output_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(output_schema["actorOutputSchemaVersion"], 1)
        self.assertIn("results", output_schema["properties"])

        input_schema = json.loads((REPO_ROOT / ".actor" / "input_schema.json").read_text(encoding="utf-8"))
        self.assertIn("action", input_schema["properties"])
        self.assertEqual(
            input_schema["properties"]["action"]["enum"],
            ["fetch", "screenshot", "inspect", "batch_fetch", "bulk_inspect", "map", "search_leads"],
        )

        ppe = json.loads((REPO_ROOT / ".actor" / "pay_per_event.json").read_text(encoding="utf-8"))
        self.assertEqual(ppe["actor-start"]["eventPriceUsd"], 0.0)
        self.assertEqual(ppe["page-fetched"]["eventPriceUsd"], 0.01)
        self.assertEqual(ppe["site-inspected"]["eventPriceUsd"], 0.01)
        self.assertEqual(ppe["screenshot-captured"]["eventPriceUsd"], 0.02)

        root_reqs = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("apify", root_reqs.lower())
        apify_reqs = (REPO_ROOT / "deploy" / "apify" / "requirements-apify.txt").read_text(encoding="utf-8")
        self.assertIn("apify", apify_reqs.lower())


class ApifyStandardRunTests(unittest.TestCase):
    def test_standard_run_fetch_charges_page_fetched(self):
        fake_actor = FakeApifyActor({"action": "fetch", "url": "https://example.com"})
        with patch("smartfetch.apify_actor.smart_fetch") as mock_fetch:
            mock_fetch.return_value = {
                "success": True,
                "requested_url": "https://example.com",
                "final_url": "https://example.com/",
                "status_code": 200,
                "title": "Example",
                "markdown": "# Example",
                "content": "Example",
            }
            res = asyncio.run(apify_actor.run_standard_mode(actor_cls=fake_actor))

        self.assertTrue(res["success"])
        self.assertEqual(fake_actor.charges, [("page-fetched", 1)])
        self.assertEqual(len(fake_actor.pushed_items), 1)
        self.assertEqual(fake_actor.pushed_items[0]["action"], "fetch")

    def test_standard_run_screenshot_stores_binary_in_kv_and_charges(self):
        fake_actor = FakeApifyActor({"action": "screenshot", "url": "https://example.com", "image_format": "png"})
        raw_png = b"\x89PNG\r\n\x1a\nFAKE"
        with patch("smartfetch.apify_actor.browser_screenshot") as mock_ss:
            mock_ss.return_value = {
                "success": True,
                "requested_url": "https://example.com",
                "final_url": "https://example.com/",
                "status_code": 200,
                "image_format": "png",
                "mime_type": "image/png",
                "image_base64": base64.b64encode(raw_png).decode("ascii"),
            }
            res = asyncio.run(apify_actor.run_standard_mode(actor_cls=fake_actor))

        self.assertTrue(res["success"])
        self.assertEqual(res["screenshot_key"], "screenshot.png")
        self.assertIn("screenshot.png", fake_actor.kv_store)
        self.assertEqual(fake_actor.kv_store["screenshot.png"], (raw_png, "image/png"))
        self.assertEqual(fake_actor.charges, [("screenshot-captured", 1)])

    def test_standard_run_inspect_charges_site_inspected(self):
        fake_actor = FakeApifyActor({"action": "inspect", "url": "https://example.com"})
        with patch("smartfetch.apify_actor.inspect_webpage_url") as mock_inspect:
            mock_inspect.return_value = {
                "success": True,
                "requested_url": "https://example.com",
                "final_url": "https://example.com/",
                "status_code": 200,
                "metadata": {"title": "Example"},
            }
            res = asyncio.run(apify_actor.run_standard_mode(actor_cls=fake_actor))

        self.assertTrue(res["success"])
        self.assertEqual(fake_actor.charges, [("site-inspected", 1)])
        self.assertEqual(len(fake_actor.pushed_items), 1)

    def test_standard_run_batch_fetch_free_plan_caps_to_3_urls(self):
        urls = [f"https://example.com/{i}" for i in range(5)]
        fake_actor = FakeApifyActor({"action": "batch_fetch", "urls": urls})
        with patch("smartfetch.apify_actor.batch_fetch_urls") as mock_batch:
            mock_batch.side_effect = lambda u_list, **kw: {
                "success": True,
                "results": [{"success": True, "url": u} for u in u_list],
                "total_urls": len(u_list),
                "succeeded_count": len(u_list),
                "failed_count": 0,
            }
            res = asyncio.run(
                apify_actor.run_standard_mode(
                    actor_cls=fake_actor,
                    environ={"APIFY_USER_IS_PAYING": "false"},
                )
            )

        self.assertTrue(res["success"])
        self.assertTrue(res["free_plan_batch_capped"])
        self.assertEqual(res["total_urls"], 3)
        self.assertEqual(fake_actor.charges, [("page-fetched", 3)])

    def test_standard_run_zero_charge_on_failure(self):
        fake_actor = FakeApifyActor({"action": "fetch", "url": "https://example.com/fail"})
        with patch("smartfetch.apify_actor.smart_fetch", side_effect=RuntimeError("Upstream failed")):
            res = asyncio.run(apify_actor.run_standard_mode(actor_cls=fake_actor))

        self.assertFalse(res["success"])
        self.assertEqual(fake_actor.charges, [])
        self.assertEqual(len(fake_actor.pushed_items), 1)
        self.assertFalse(fake_actor.pushed_items[0]["success"])

    def test_standard_run_bulk_inspect_normalizes_domains_and_charges_only_succeeded(self):
        fake_actor = FakeApifyActor(
            {
                "action": "bulk_inspect",
                "urls": ["stripe.com", "https://linear.app", "fail.example.com"],
            }
        )

        def fake_inspect(target_url: str, **_kw):
            if "fail.example.com" in target_url:
                raise RuntimeError("DNS failed")
            return {
                "success": True,
                "requested_url": target_url,
                "final_url": f"{target_url}/",
                "status_code": 200,
                "metadata": {"title": f"Title {target_url}", "description": "B2B SaaS"},
                "contacts": {
                    "emails": ["sales@example.com", "support@example.com"],
                    "phones": ["+1-800-555-0199"],
                    "social_links": {
                        "github": "https://github.com/example",
                        "linkedin": "https://linkedin.com/company/example",
                        "x_twitter": "https://x.com/example",
                    },
                },
                "tech_stack": ["Next.js", "Stripe", "Cloudflare"],
                "security_headers": {
                    "https": True,
                    "strict_transport_security": "max-age=31536000",
                    "content_security_policy": None,
                },
            }

        with patch("smartfetch.apify_actor.inspect_webpage_url", side_effect=fake_inspect):
            res = asyncio.run(
                apify_actor.run_standard_mode(
                    actor_cls=fake_actor,
                    environ={"APIFY_USER_IS_PAYING": "true"},
                )
            )

        self.assertTrue(res["success"])
        self.assertEqual(res["total_urls"], 3)
        self.assertEqual(res["succeeded_count"], 2)
        self.assertEqual(res["failed_count"], 1)
        # Strictly charges 2 site-inspected events (0 for the failed domain)
        self.assertEqual(fake_actor.charges, [("site-inspected", 2)])
        self.assertEqual(len(fake_actor.pushed_items), 3)

        first = fake_actor.pushed_items[0]
        self.assertEqual(first["action"], "bulk_inspect")
        self.assertEqual(first["domain"], "stripe.com")
        self.assertEqual(first["requested_url"], "https://stripe.com")
        self.assertEqual(first["emails"], "sales@example.com, support@example.com")
        self.assertEqual(first["tech_stack"], "Next.js, Stripe, Cloudflare")
        self.assertTrue(first["https"])
        self.assertTrue(first["has_hsts"])
        self.assertFalse(first["has_csp"])

        failed = fake_actor.pushed_items[2]
        self.assertFalse(failed["success"])
        self.assertIn("DNS failed", failed["error"])

        # Validate every pushed row against .actor/dataset_schema.json
        import jsonschema
        dataset_schema = json.loads((REPO_ROOT / ".actor" / "dataset_schema.json").read_text(encoding="utf-8"))
        for row in fake_actor.pushed_items:
            jsonschema.validate(instance=row, schema=dataset_schema["fields"])

    def test_standard_run_bulk_inspect_free_plan_caps_to_5_urls(self):
        domains = [f"company{i}.com" for i in range(8)]
        fake_actor = FakeApifyActor({"action": "bulk_inspect", "urls": domains})
        with patch("smartfetch.apify_actor.inspect_webpage_url") as mock_inspect:
            mock_inspect.side_effect = lambda u, **_kw: {
                "success": True,
                "requested_url": u,
                "final_url": u,
                "status_code": 200,
                "metadata": {"title": "Co"},
                "contacts": {"emails": [], "phones": [], "social_links": {}},
                "tech_stack": [],
                "security_headers": {"https": True},
            }
            res = asyncio.run(
                apify_actor.run_standard_mode(
                    actor_cls=fake_actor,
                    environ={"APIFY_USER_IS_PAYING": "false"},
                )
            )

        self.assertTrue(res["success"])
        self.assertTrue(res["free_plan_batch_capped"])
        self.assertEqual(res["total_urls"], 5)
        self.assertEqual(fake_actor.charges, [("site-inspected", 5)])


class ApifyStandbyMCPTests(unittest.TestCase):
    def test_standby_readiness_probe_and_mcp_tool_execution(self):
        fake_actor = FakeApifyActor()
        app = apify_actor.create_standby_app(
            actor_cls=fake_actor,
            environ={"APIFY_USER_IS_PAYING": "true"},
        )
        with patch("smartfetch.apify_actor.smart_fetch") as mock_fetch:
            mock_fetch.return_value = {
                "success": True,
                "requested_url": "https://example.com",
                "final_url": "https://example.com/",
                "status_code": 200,
                "title": "Standby Example",
                "markdown": "# Standby Example",
                "content": "Standby Example",
            }
            with TestClient(app) as client:
                # 1. Apify readiness probe on GET /
                probe_res = client.get("/", headers={"x-apify-container-server-readiness-probe": "1"})
                self.assertEqual(probe_res.status_code, 200)
                probe_json = probe_res.json()
                self.assertTrue(probe_json["ok"])
                self.assertTrue(probe_json["readiness_probe"])
                self.assertEqual(probe_json["mcp_path"], "/mcp")

                # 2. MCP tools/list
                list_res = client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                self.assertEqual(list_res.status_code, 200)
                tool_names = [t["name"] for t in list_res.json()["result"]["tools"]]
                self.assertEqual(
                    set(tool_names),
                    {
                        "fetch_webpage",
                        "screenshot_webpage",
                        "inspect_webpage",
                        "batch_fetch_webpages",
                        "map_website",
                    },
                )

                # 3. MCP tools/call fetch_webpage charges page-fetched
                call_res = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "fetch_webpage",
                            "arguments": {"url": "https://example.com"},
                        },
                    },
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                self.assertEqual(call_res.status_code, 200)
                body = call_res.json()["result"]
                self.assertFalse(body["isError"])
                self.assertEqual(fake_actor.charges, [("page-fetched", 1)])

    def test_sys_modules_apify_import_fallback(self):
        fake_actor = FakeApifyActor({"action": "fetch", "url": "https://example.com"})
        mock_apify_mod = MagicMock()
        mock_apify_mod.Actor = fake_actor
        with patch.dict("sys.modules", {"apify": mock_apify_mod}):
            with patch("smartfetch.apify_actor.smart_fetch") as mock_fetch:
                mock_fetch.return_value = {
                    "success": True,
                    "requested_url": "https://example.com",
                    "final_url": "https://example.com/",
                    "status_code": 200,
                    "title": "SysMod Example",
                    "markdown": "# SysMod Example",
                    "content": "SysMod Example",
                }
                res = asyncio.run(apify_actor.run_standard_mode())
        self.assertTrue(res["success"])
        self.assertEqual(fake_actor.charges, [("page-fetched", 1)])


if __name__ == "__main__":
    unittest.main()
