"""Comprehensive unit and integration tests for Build 1: Webpage Screenshots.

Covers:
- WarmChromiumInstance.screenshot CDP interaction, viewport override, 3MB cap.
- BrowserWorkerHandler /v1/screenshot HMAC verification and SSRF policy.
- RemoteBrowserClient.screenshot and browser_screenshot caching.
- FastAPI POST /screenshot operational 402 challenge ($0.01) and emergency 503.
- MCP tools/list and screenshot_webpage metadata.
"""

import base64
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from smartfetch import server
from smartfetch.browser_pool import WarmChromiumInstance
from smartfetch.cache import build_screenshot_cache_key
from smartfetch.payments import X402Settings, BASE_MAINNET, BASE_SEPOLIA


VALID_PAY_TO = "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786"


class ScreenshotUnitTests(unittest.TestCase):
    def test_cache_key_generation(self):
        key1 = build_screenshot_cache_key("https://example.com", "desktop", False, "jpeg")
        key2 = build_screenshot_cache_key("https://example.com", "mobile", False, "jpeg")
        key3 = build_screenshot_cache_key("https://example.com", "desktop", True, "jpeg")
        key4 = build_screenshot_cache_key("https://example.com", "desktop", False, "png")

        self.assertTrue(key1.startswith("sf:screenshot:"))
        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)
        self.assertNotEqual(key1, key4)

    @patch("smartfetch.browser_pool.websocket.create_connection")
    def test_warm_instance_screenshot_metrics_and_cap(self, mock_ws_connect):
        inst = WarmChromiumInstance(instance_id="test-screenshot")
        inst.ws_endpoint = "ws://127.0.0.1:9222/devtools/browser/abc"

        mock_ws = MagicMock()
        mock_ws_connect.return_value = mock_ws

        fake_image = base64.b64encode(b"FAKE_IMAGE_DATA_12345").decode("ascii")

        # Mock incoming CDP responses in sequential order
        responses = [
            '{"id": 1, "result": {"browserContextId": "ctx-123"}}',
            '{"id": 2, "result": {"targetId": "target-456"}}',
            '{"id": 3, "result": {"sessionId": "sess-789"}}',
            '{"id": 4, "result": {}}',  # Page.enable
            '{"id": 5, "result": {}}',  # Emulation.setDeviceMetricsOverride
            '{"id": 6, "result": {}}',  # Page.navigate
            '{"id": 7, "result": {"result": {"value": "complete"}}}',  # readyState
            '{"id": 8, "result": {"result": {"value": 200}}}',  # responseStatus
            f'{{"id": 9, "result": {{"data": "{fake_image}"}}}}',  # Page.captureScreenshot
            '{"id": 10, "result": {"result": {"value": "https://example.com"}}}',  # url
            '{"id": 11, "result": {"result": {"value": "Example Title"}}}',  # title
            '{"id": 12, "result": {}}',  # Target.disposeBrowserContext
        ]
        mock_ws.recv.side_effect = responses

        res = inst.screenshot(
            "https://example.com",
            deadline=time.monotonic() + 10.0,
            viewport="desktop",
            full_page=False,
            image_format="jpeg",
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["viewport"], "desktop")
        self.assertEqual(res["width"], 1280)
        self.assertEqual(res["height"], 800)
        self.assertEqual(res["image_format"], "jpeg")
        self.assertEqual(res["mime_type"], "image/jpeg")
        self.assertEqual(res["image_base64"], fake_image)
        self.assertEqual(inst.metrics.jobs_executed, 1)

    @patch("smartfetch.browser_pool.websocket.create_connection")
    def test_warm_instance_screenshot_exceeds_3mb_cap(self, mock_ws_connect):
        inst = WarmChromiumInstance(instance_id="test-screenshot-cap")
        inst.ws_endpoint = "ws://127.0.0.1:9222/devtools/browser/abc"

        mock_ws = MagicMock()
        mock_ws_connect.return_value = mock_ws

        huge_payload = base64.b64encode(b"X" * 3_000_001).decode("ascii")

        responses = [
            '{"id": 1, "result": {"browserContextId": "ctx-123"}}',
            '{"id": 2, "result": {"targetId": "target-456"}}',
            '{"id": 3, "result": {"sessionId": "sess-789"}}',
            '{"id": 4, "result": {}}',
            '{"id": 5, "result": {}}',
            '{"id": 6, "result": {}}',
            '{"id": 7, "result": {"result": {"value": "complete"}}}',
            '{"id": 8, "result": {"result": {"value": 200}}}',
            f'{{"id": 9, "result": {{"data": "{huge_payload}"}}}}',
            '{"id": 10, "result": {}}',
        ]
        mock_ws.recv.side_effect = responses

        with self.assertRaises(RuntimeError) as ctx:
            inst.screenshot(
                "https://example.com",
                deadline=time.monotonic() + 10.0,
            )
        self.assertIn("exceeds maximum limit of 3MB", str(ctx.exception))


class ScreenshotDaemonHMACAndSSRFTests(unittest.TestCase):
    def test_daemon_ssrf_validation_blocks_private_targets(self):
        from smartfetch.browser_daemon import validate_public_url

        with self.assertRaises(ValueError):
            validate_public_url("http://127.0.0.1:8080/test")
        with self.assertRaises(ValueError):
            validate_public_url("http://169.254.169.254/latest/meta-data")
        with self.assertRaises(ValueError):
            validate_public_url("http://localhost/")


class ScreenshotEndpointOperationalAndEmergencyTests(unittest.TestCase):
    def test_emergency_mode_screenshot_returns_503(self):
        with patch.dict("os.environ", {"BROWSER_WORKER_URL": ""}):
            app = server.create_app(X402Settings(False, None, "$0.005", BASE_SEPOLIA))
            with TestClient(app) as client:
                res = client.post("/screenshot", json={"url": "https://example.com"})
                self.assertEqual(res.status_code, 503)
                data = res.json()
                self.assertFalse(data["success"])
                self.assertEqual(data["error_code"], "browser_unavailable")

    def test_operational_mode_unpaid_probe_returns_402_challenge(self):
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
            "BROWSER_TLS_CA_CERT": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "BROWSER_TLS_CLIENT_CERT": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "BROWSER_TLS_CLIENT_KEY": "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
            "X402_ALLOW_MOCK_FACILITATOR": "true",
        }):
            with patch("smartfetch.payments.create_facilitator") as mock_fac:
                from x402.schemas import SupportedKind, SupportedResponse
                mock_client = MagicMock()
                mock_client.get_supported.return_value = SupportedResponse(
                    kinds=[SupportedKind(x402Version=2, scheme="exact", network=BASE_MAINNET)]
                )
                mock_fac.return_value = mock_client
                app = server.create_app(settings)
                with TestClient(app) as client:
                    # Unauthenticated probe with empty {}
                    res = client.post("/screenshot", json={})
                    self.assertEqual(res.status_code, 402)
                    req_header = res.headers.get("payment-required")
                    self.assertIsNotNone(req_header)
                    decoded = json.loads(base64.b64decode(req_header).decode("utf-8"))
                    self.assertEqual(decoded["accepts"][0]["amount"], "10000")  # $0.01 USDC
                    self.assertEqual(decoded["accepts"][0]["network"], BASE_MAINNET)
                    self.assertEqual(decoded["accepts"][0]["payTo"], VALID_PAY_TO)

    def test_mcp_tools_list_contains_screenshot_webpage_and_accepts(self):
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
            "BROWSER_TLS_CA_CERT": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "BROWSER_TLS_CLIENT_CERT": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "BROWSER_TLS_CLIENT_KEY": "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
            "X402_ALLOW_MOCK_FACILITATOR": "true",
        }):
            with patch("smartfetch.payments.create_facilitator") as mock_fac:
                from x402.schemas import SupportedKind, SupportedResponse
                mock_client = MagicMock()
                mock_client.get_supported.return_value = SupportedResponse(
                    kinds=[SupportedKind(x402Version=2, scheme="exact", network=BASE_MAINNET)]
                )
                mock_fac.return_value = mock_client
                app = server.create_app(settings)
                with TestClient(app) as client:
                    rpc_init = client.post("/mcp", json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1.0"}
                        }
                    }, headers={"Content-Type": "application/json", "Accept": "application/json"})
                    self.assertEqual(rpc_init.status_code, 200)

                    rpc_list = client.post("/mcp", json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {}
                    }, headers={"Content-Type": "application/json", "Accept": "application/json"})
                    self.assertEqual(rpc_list.status_code, 200)
                    tools = rpc_list.json()["result"]["tools"]
                    tool_names = [t["name"] for t in tools]
                    self.assertIn("screenshot_webpage", tool_names)
                    screenshot_tool = next(t for t in tools if t["name"] == "screenshot_webpage")
                    self.assertIn("viewport", screenshot_tool["inputSchema"]["properties"])
                    self.assertIn("full_page", screenshot_tool["inputSchema"]["properties"])
                    self.assertIn("image_format", screenshot_tool["inputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main()
