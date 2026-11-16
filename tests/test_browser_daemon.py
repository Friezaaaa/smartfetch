"""Unit and integration tests for the VPS Browser Worker Daemon."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from smartfetch.browser_daemon import (
    BrowserDaemonConfig,
    BrowserWorkerDaemon,
    NonceReplayCache,
    WorkerRunner,
    create_daemon,
)
from smartfetch.remote_browser import (
    ERROR_BROWSER_UNAVAILABLE,
    ERROR_INVALID_REQUEST,
    ERROR_POLICY_REJECTION,
    ERROR_RENDER_FAILED,
    ERROR_TIMEOUT,
    RemoteBrowserClient,
    RemoteBrowserConfig,
    RemoteBrowserError,
    sign_request,
)


class MockWorkerRunner(WorkerRunner):
    """Controllable worker runner for daemon tests."""

    def __init__(self):
        self.should_timeout = False
        self.should_fail = False
        self.rendered_content = "Mock Rendered Text"
        self.calls = []

    def run(
        self,
        url: str,
        *,
        socket_path: str,
        operation_id: bytes,
        deadline: float,
        max_chars: int,
        timeout_seconds: float,
    ) -> dict:
        self.calls.append((url, socket_path, operation_id))
        if self.should_timeout:
            raise TimeoutError("Simulated worker timeout")
        if self.should_fail:
            raise RuntimeError("Simulated worker crash")

        return {
            "success": True,
            "requested_url": url,
            "final_url": url,
            "status_code": 200,
            "render_method": "browser",
            "elapsed_ms": 120,
            "content": self.rendered_content,
            "title": "Mock Title",
            "description": "Mock Description",
            "markdown": f"# {self.rendered_content}",
            "html": f"<html><body><h1>{self.rendered_content}</h1></body></html>",
        }


class TestNonceReplayCache(unittest.TestCase):
    def test_nonce_fresh_and_replay(self):
        cache = NonceReplayCache(ttl_seconds=1.0)
        nonce = "abc123nonce00000000000000000000"
        self.assertTrue(cache.check_and_add(nonce))
        # Second call with same nonce must be rejected
        self.assertFalse(cache.check_and_add(nonce))

    def test_nonce_expires_after_ttl(self):
        cache = NonceReplayCache(ttl_seconds=0.1)
        nonce = "expiring_nonce_1234567890123456"
        self.assertTrue(cache.check_and_add(nonce))
        time.sleep(0.15)
        # After TTL, nonce should be accepted again
        self.assertTrue(cache.check_and_add(nonce))


class TestBrowserDaemonEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_ipc = tempfile.mkdtemp(prefix="smartfetch-daemon-test-")
        cls.hmac_key = "test-daemon-hmac-key-32-bytes-long!"
        cls.mock_runner = MockWorkerRunner()

        # Find an open port
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]

        cls.config = BrowserDaemonConfig(
            host="127.0.0.1",
            port=cls.port,
            hmac_key=cls.hmac_key,
            max_concurrent=2,
            ipc_dir=cls.temp_ipc,
        )

        def test_validator(url):
            if "127.0.0.1" in url or "169.254" in url:
                raise ValueError("Target rejected by policy")
            return url

        cls.test_validator = staticmethod(test_validator)
        cls.server = create_daemon(cls.config, runner=cls.mock_runner, url_validator=test_validator)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

        # Build RemoteBrowserClient pointing to our test daemon
        cls.client_config = RemoteBrowserConfig(
            worker_url=f"http://127.0.0.1:{cls.port}",
            hmac_key=cls.hmac_key,
            connect_timeout=2.0,
            render_timeout=5.0,
        )
        cls.client = RemoteBrowserClient(cls.client_config)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        import shutil
        shutil.rmtree(cls.temp_ipc, ignore_errors=True)

    def setUp(self):
        self.mock_runner.should_timeout = False
        self.mock_runner.should_fail = False
        self.mock_runner.calls.clear()
        self.client.circuit_breaker.record_success()
        # Wait for any in-flight request cleanup from previous test
        deadline = time.monotonic() + 2.0
        while self.server.active_count > 0 and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_health_check(self):
        resp = requests.get(f"http://127.0.0.1:{self.port}/health", timeout=2)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("service"), "smartfetch-browser-worker")
        self.assertEqual(data.get("capacity"), 2)

    def test_missing_auth_rejected(self):
        payload = json.dumps({"url": "https://example.com"}).encode("utf-8")
        resp = requests.post(f"http://127.0.0.1:{self.port}/v1/render", data=payload, timeout=2)
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("error_code"), ERROR_INVALID_REQUEST)

    def test_tampered_signature_rejected(self):
        payload = json.dumps({"url": "https://example.com"}).encode("utf-8")
        headers = sign_request("POST", "/v1/render", payload, self.hmac_key)
        headers["X-SmartFetch-Signature"] = "bad" + headers["X-SmartFetch-Signature"][3:]
        resp = requests.post(f"http://127.0.0.1:{self.port}/v1/render", data=payload, headers=headers, timeout=2)
        self.assertEqual(resp.status_code, 401)

    def test_replayed_nonce_rejected(self):
        payload = json.dumps({"url": "https://example.com"}).encode("utf-8")
        headers = sign_request("POST", "/v1/render", payload, self.hmac_key)
        # First request: passed through or validated
        # Even if rejected at policy later, the nonce is registered
        requests.post(f"http://127.0.0.1:{self.port}/v1/render", data=payload, headers=headers, timeout=2)

        # Second request with identical nonce must return 401
        resp2 = requests.post(f"http://127.0.0.1:{self.port}/v1/render", data=payload, headers=headers, timeout=2)
        self.assertEqual(resp2.status_code, 401)
        data2 = resp2.json()
        self.assertIn("nonce", data2.get("error", "").lower())

    def test_private_ip_ssrf_policy_rejection(self):
        payload = json.dumps({"url": "http://127.0.0.1:8080/admin"}).encode("utf-8")
        headers = sign_request("POST", "/v1/render", payload, self.hmac_key)
        resp = requests.post(f"http://127.0.0.1:{self.port}/v1/render", data=payload, headers=headers, timeout=2)
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertEqual(data.get("error_code"), ERROR_POLICY_REJECTION)

    def test_cloud_metadata_policy_rejection(self):
        payload = json.dumps({"url": "http://169.254.169.254/latest/meta-data"}).encode("utf-8")
        headers = sign_request("POST", "/v1/render", payload, self.hmac_key)
        resp = requests.post(f"http://127.0.0.1:{self.port}/v1/render", data=payload, headers=headers, timeout=2)
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertEqual(data.get("error_code"), ERROR_POLICY_REJECTION)

    def test_end_to_end_successful_render_via_client(self):
        result = self.client.render("https://example.com", _url_validator=self.test_validator)
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("final_url"), "https://example.com")
        self.assertEqual(result.get("render_method"), "browser")
        self.assertEqual(result.get("content"), "Mock Rendered Text")
        self.assertIn("html", result)
        self.assertIn("diagnostics", result)
        self.assertIn("broker_bytes", result["diagnostics"])

    def test_worker_timeout_mapped_to_504(self):
        self.mock_runner.should_timeout = True
        with self.assertRaises(RemoteBrowserError) as ctx:
            self.client.render("https://example.com", _url_validator=self.test_validator)
        self.assertEqual(ctx.exception.code, ERROR_TIMEOUT)
        self.assertEqual(ctx.exception.phase, "browser_navigate")

    def test_worker_crash_mapped_to_500(self):
        self.mock_runner.should_fail = True
        with self.assertRaises(RemoteBrowserError) as ctx:
            self.client.render("https://example.com", _url_validator=self.test_validator)
        self.assertEqual(ctx.exception.code, ERROR_RENDER_FAILED)

    def test_capacity_exhaustion_returns_503(self):
        # Acquire all semaphore slots
        self.server.semaphore.acquire()
        self.server.semaphore.acquire()
        try:
            with self.assertRaises(RemoteBrowserError) as ctx:
                self.client.render("https://example.com", _url_validator=self.test_validator)
            self.assertEqual(ctx.exception.code, ERROR_BROWSER_UNAVAILABLE)
            self.assertEqual(ctx.exception.phase, "capacity")
        finally:
            self.server.semaphore.release()
            self.server.semaphore.release()


if __name__ == "__main__":
    unittest.main()
