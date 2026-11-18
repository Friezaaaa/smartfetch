"""Unit tests for Upgrade A: Tier 1 Chrome TLS impersonation, Chromium stealth & smart settling,
pre-capture controls, and optional SSRF-safe Broker upstream proxy hook."""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from smartfetch import emergency_request, http_fetch
from smartfetch.browser_pool import WarmChromiumInstance, normalize_capture_options
from smartfetch.browser_prototype.broker import Broker
from smartfetch.browser_prototype.policy import DestinationPolicy
from smartfetch.security import PinnedTarget


class CurlCffiTier1Tests(unittest.TestCase):
    def test_curl_cffi_pins_resolved_public_ip_and_blocks_private_targets(self):
        # Private/loopback/metadata targets must still be rejected before any curl_cffi call
        with patch("smartfetch.http_fetch.curl_requests.get") as mock_curl_get:
            for bad_url in (
                "http://127.0.0.1/admin",
                "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/",
            ):
                with self.subTest(url=bad_url):
                    with self.assertRaises(ValueError):
                        http_fetch.http_fetch(bad_url)
            mock_curl_get.assert_not_called()

    def test_curl_cffi_uses_curlopt_resolve_and_enforces_max_bytes(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        fake_resp.encoding = "utf-8"
        fake_resp.iter_content.return_value = [b"<html><body>Hello Chrome TLS</body></html>"]

        pinned = PinnedTarget(
            url="https://example.com/",
            hostname="example.com",
            port=443,
            scheme="https",
            addresses=("93.184.216.34",),
        )
        with (
            patch("smartfetch.http_fetch.resolve_and_pin_public_url", return_value=pinned),
            patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u),
            patch("smartfetch.http_fetch.curl_requests.get", return_value=fake_resp) as mock_curl_get,
        ):
            res = http_fetch.http_fetch("https://example.com/")

        self.assertEqual(res["status_code"], 200)
        self.assertIn("Hello Chrome TLS", res["html"])
        mock_curl_get.assert_called_once()
        kwargs = mock_curl_get.call_args.kwargs
        self.assertEqual(kwargs["impersonate"], "chrome")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["proxies"], {})
        from curl_cffi import CurlOpt

        self.assertEqual(kwargs["curl_options"][CurlOpt.RESOLVE], ["example.com:443:93.184.216.34"])

    def test_curl_cffi_enforces_max_bytes_streaming_limit(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.headers = {"Content-Type": "text/html"}
        fake_resp.iter_content.return_value = [b"A" * 600, b"B" * 600]

        pinned = PinnedTarget(
            url="https://example.com/",
            hostname="example.com",
            port=443,
            scheme="https",
            addresses=("93.184.216.34",),
        )
        with (
            patch("smartfetch.http_fetch.MAX_BYTES", 1000),
            patch("smartfetch.http_fetch.resolve_and_pin_public_url", return_value=pinned),
            patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u),
            patch("smartfetch.http_fetch.curl_requests.get", return_value=fake_resp),
        ):
            with self.assertRaisesRegex(RuntimeError, "byte limit"):
                http_fetch.http_fetch("https://example.com/")

    def test_curl_cffi_blocks_redirect_to_private_ip(self):
        redir_resp = MagicMock()
        redir_resp.status_code = 302
        redir_resp.headers = {"Location": "http://127.0.0.1/secret"}

        pinned = PinnedTarget(
            url="https://example.com/",
            hostname="example.com",
            port=443,
            scheme="https",
            addresses=("93.184.216.34",),
        )
        with (
            patch("smartfetch.http_fetch.resolve_and_pin_public_url", return_value=pinned),
            patch("smartfetch.http_fetch.curl_requests.get", return_value=redir_resp) as mock_curl_get,
            patch("smartfetch.http_fetch.validate_public_url", side_effect=["https://example.com/", ValueError("Private/local/network-metadata targets are blocked")]),
        ):
            with self.assertRaises(ValueError):
                http_fetch.http_fetch("https://example.com/")
        self.assertEqual(mock_curl_get.call_count, 1)


class WarmChromiumStealthTests(unittest.TestCase):
    def test_start_adds_stealth_and_window_size_flags(self):
        captured_cmds: list[list[str]] = []

        class FakeProc:
            pid = 4242
            stderr = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                pass

        def fake_popen(cmd, **_kw):
            captured_cmds.append(cmd)
            # Write DevToolsActivePort inside profile dir
            user_data = [a.split("=", 1)[1] for a in cmd if a.startswith("--user-data-dir=")][0]
            with open(f"{user_data}/DevToolsActivePort", "w", encoding="utf-8") as f:
                f.write("9222\n/devtools/browser/test\n")
            return FakeProc()

        inst = WarmChromiumInstance("stealth-test")
        with patch("smartfetch.browser_pool.subprocess.Popen", side_effect=fake_popen):
            inst.start(timeout=2.0)
        inst.close()

        self.assertEqual(len(captured_cmds), 1)
        flags = captured_cmds[0]
        self.assertIn("--disable-blink-features=AutomationControlled", flags)
        self.assertIn("--window-size=1280,800", flags)

    def test_render_wait_ready_applies_stealth_cf_settle_stability_selector_scroll_and_cookies(self):
        inst = WarmChromiumInstance("render-test")
        inst.ws_endpoint = "ws://127.0.0.1:9222/devtools/browser/test"

        cdp_calls: list[tuple[str, dict]] = []
        eval_expressions: list[str] = []

        cf_checks = {"n": 0}
        len_checks = {"n": 0}
        sel_checks = {"n": 0}

        class FakeWS:
            def __init__(self):
                self._queue: list[str] = []

            def settimeout(self, _t):
                pass

            def close(self):
                pass

            def send(self, raw: str):
                msg = json.loads(raw)
                call_id = msg["id"]
                method = msg["method"]
                params = msg.get("params", {})
                cdp_calls.append((method, params))

                result: dict = {}
                if method == "Target.createBrowserContext":
                    result = {"browserContextId": "ctx-1"}
                elif method == "Target.createTarget":
                    result = {"targetId": "tgt-1"}
                elif method == "Target.attachToTarget":
                    result = {"sessionId": "sess-1"}
                elif method == "Runtime.evaluate":
                    expr = params.get("expression", "")
                    eval_expressions.append(expr)
                    if expr == "document.readyState":
                        result = {"result": {"value": "complete"}}
                    elif "document.title" in expr and "innerText.slice" in expr:
                        cf_checks["n"] += 1
                        val = "just a moment" if cf_checks["n"] == 1 else "loaded app"
                        result = {"result": {"value": val}}
                    elif "innerText.trim().length" in expr:
                        len_checks["n"] += 1
                        result = {"result": {"value": 1200}}
                    elif "document.querySelector(" in expr:
                        sel_checks["n"] += 1
                        result = {"result": {"value": sel_checks["n"] >= 2}}
                    elif "responseStatus" in expr:
                        result = {"result": {"value": 200}}
                    elif expr == "document.documentElement.outerHTML":
                        result = {"result": {"value": "<html><body><main><h1>Stealth OK</h1></main></body></html>"}}
                    elif expr == "window.location.href":
                        result = {"result": {"value": "https://example.com/"}}
                self._queue.append(json.dumps({"id": call_id, "result": result}))

            def recv(self) -> str:
                return self._queue.pop(0)

        with patch("smartfetch.browser_pool.websocket.create_connection", return_value=FakeWS()):
            res = inst.render(
                "https://example.com/",
                deadline=time.monotonic() + 10.0,
                wait_ready=True,
                wait_ms=10,
                wait_for_selector='div[data-ready="1"]',
                scroll=True,
                dismiss_cookies=True,
            )

        self.assertTrue(res["success"])
        methods = [m for m, _ in cdp_calls]
        self.assertIn("Network.setUserAgentOverride", methods)
        self.assertIn("Page.addScriptToEvaluateOnNewDocument", methods)

        ua_params = next(p for m, p in cdp_calls if m == "Network.setUserAgentOverride")
        self.assertNotIn("HeadlessChrome", ua_params["userAgent"])
        self.assertEqual(ua_params["platform"], "Win32")

        doc_script = next(p["source"] for m, p in cdp_calls if m == "Page.addScriptToEvaluateOnNewDocument")
        self.assertIn("webdriver", doc_script)
        self.assertIn("window.chrome", doc_script)

        # Verify Cloudflare marker polled twice (resolved on 2nd), stability polled twice, selector JSON-encoded
        self.assertEqual(cf_checks["n"], 2)
        self.assertEqual(len_checks["n"], 2)
        self.assertTrue(any('document.querySelector("div[data-ready=\\"1\\"]")' in e for e in eval_expressions))
        self.assertTrue(any("window.scrollTo" in e for e in eval_expressions))
        self.assertTrue(any("onetrust-banner-sdk" in e for e in eval_expressions))

    def test_normalize_capture_options_bounds(self):
        wait_ms, sel, scroll, dismiss = normalize_capture_options(
            wait_ms=99999,
            wait_for_selector="  " + ("a" * 300),
            scroll=1,
            dismiss_cookies=0,
        )
        self.assertEqual(wait_ms, 5000)
        self.assertEqual(len(sel), 256)
        self.assertTrue(scroll)
        self.assertFalse(dismiss)


class BrokerUpstreamProxyTests(unittest.TestCase):
    def test_broker_enforces_ssrf_before_dialing_upstream_proxy_and_tunnels_public_connect(self):
        # Spin up a tiny mock HTTP proxy that logs incoming CONNECT / GET requests
        received_requests: list[str] = []
        proxy_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy_srv.bind(("127.0.0.1", 0))
        proxy_srv.listen(4)
        proxy_port = proxy_srv.getsockname()[1]
        stop_flag = threading.Event()

        def proxy_loop():
            proxy_srv.settimeout(0.2)
            while not stop_flag.is_set():
                try:
                    conn, _ = proxy_srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with conn:
                    data = conn.recv(4096).decode("ascii", errors="replace")
                    received_requests.append(data)
                    if data.startswith("CONNECT "):
                        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        echo = conn.recv(1024)
                        conn.sendall(b"TUNNELED:" + echo)
                    else:
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")

        t = threading.Thread(target=proxy_loop, daemon=True)
        t.start()

        broker = Broker(
            DestinationPolicy(),
            resolver=lambda host, port: ["93.184.216.34"] if host == "example.com" else ["127.0.0.1"],
            total_deadline=time.monotonic() + 5.0,
            upstream_proxy_url=f"http://user1:secret1@127.0.0.1:{proxy_port}",
        )
        host, port = broker.start()
        try:
            # 1. SSRF target MUST return 403 and NEVER dial upstream proxy!
            with socket.create_connection((host, port), timeout=2.0) as s:
                s.sendall(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\nHost: 169.254.169.254:80\r\n\r\n")
                resp = s.recv(1024).decode("ascii")
            self.assertIn("403", resp)
            self.assertEqual(received_requests, [])

            with socket.create_connection((host, port), timeout=2.0) as s:
                s.sendall(b"GET http://127.0.0.1:80/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                resp = s.recv(1024).decode("ascii")
            self.assertIn("403", resp)
            self.assertEqual(received_requests, [])

            # 2. Public target tunnels through upstream proxy with Proxy-Authorization
            with socket.create_connection((host, port), timeout=2.0) as s:
                s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
                resp = s.recv(1024)
                self.assertIn(b"200 Connection Established", resp)
                s.sendall(b"PING")
                tunneled = s.recv(1024)
                self.assertEqual(tunneled, b"TUNNELED:PING")

            self.assertEqual(len(received_requests), 1)
            self.assertIn("CONNECT example.com:443 HTTP/1.1", received_requests[0])
            self.assertIn("Proxy-Authorization: Basic ", received_requests[0])
        finally:
            broker.close()
            stop_flag.set()
            proxy_srv.close()
            t.join(timeout=1.0)


class EmergencyRequestCaptureBoundsTests(unittest.TestCase):
    def test_wait_ms_and_wait_for_selector_bounds_enforced(self):
        headers = ((b"content-type", b"application/json"),)

        # Valid bounds pass
        valid_body = json.dumps({
            "url": "https://example.com/",
            "wait_ms": 1500,
            "wait_for_selector": "#main",
            "scroll": True,
            "dismiss_cookies": False,
        }).encode("utf-8")
        call = emergency_request.classify_fetch(valid_body, headers)
        self.assertEqual(call.tool, "fetch_webpage")

        ss_call = emergency_request.classify_screenshot(valid_body, headers)
        self.assertEqual(ss_call.tool, "screenshot_webpage")

        # wait_ms > 5000 rejected with 422
        bad_wait = json.dumps({"url": "https://example.com/", "wait_ms": 5001}).encode("utf-8")
        with self.assertRaises(emergency_request.EmergencyRequestError) as ctx:
            emergency_request.classify_fetch(bad_wait, headers)
        self.assertEqual(ctx.exception.status, 422)

        # wait_for_selector > 256 chars rejected with 422
        bad_sel = json.dumps({"url": "https://example.com/", "wait_for_selector": "a" * 257}).encode("utf-8")
        with self.assertRaises(emergency_request.EmergencyRequestError) as ctx2:
            emergency_request.classify_screenshot(bad_sel, headers)
        self.assertEqual(ctx2.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
