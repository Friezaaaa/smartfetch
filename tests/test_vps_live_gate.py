"""Automated test gate verifying the live isolated VPS browser worker daemon.

Verifies:
1. mTLS handshake and /health response
2. HMAC authentication and timestamp enforcement (401 on missing/bad signature)
3. mTLS enforcement (fails closed without valid client certificate)
4. SSRF protection via DestinationPolicy (blocks loopback & cloud metadata)
5. Live headless Chromium rendering with real DOM and title extraction
6. Fail-closed error handling and circuit breaker behavior
"""

from __future__ import annotations

import os
from pathlib import Path
import time
import unittest

import requests

from smartfetch.remote_browser import (
    ERROR_BROWSER_UNAVAILABLE,
    ERROR_POLICY_REJECTION,
    RemoteBrowserClient,
    RemoteBrowserConfig,
    RemoteBrowserError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CERTS_DIR = REPO_ROOT / "deploy" / "vps" / "certs"


class LiveVPSDeploymentGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.worker_url = os.getenv("BROWSER_WORKER_URL", "https://159.89.40.174:8443").rstrip("/")
        cls.hmac_key = os.getenv("BROWSER_WORKER_HMAC_KEY")
        if not cls.hmac_key:
            raise unittest.SkipTest("BROWSER_WORKER_HMAC_KEY not set; skipping live gate test")
        certs_dir = Path(os.getenv("BROWSER_WORKER_CERTS_DIR", str(DEFAULT_CERTS_DIR)))
        cls.ca_cert = str(certs_dir / "ca.crt")
        cls.client_cert = str(certs_dir / "client.crt")
        cls.client_key = str(certs_dir / "client.key")

        # Skip live gate if certs are not locally available
        if not (os.path.isfile(cls.ca_cert) and os.path.isfile(cls.client_cert) and os.path.isfile(cls.client_key)):
            raise unittest.SkipTest("Live VPS certificates not found; skipping live gate test")

        cls.config = RemoteBrowserConfig(
            worker_url=cls.worker_url,
            hmac_key=cls.hmac_key,
            ca_cert_path=cls.ca_cert,
            client_cert_path=cls.client_cert,
            client_key_path=cls.client_key,
            connect_timeout=5.0,
            render_timeout=35.0,
        )
        cls.client = RemoteBrowserClient(cls.config)

    def test_01_health_check_mtls(self) -> None:
        """Health check over mTLS must return 200 with service info."""
        res = requests.get(
            f"{self.worker_url}/health",
            verify=self.ca_cert,
            cert=(self.client_cert, self.client_key),
            timeout=5.0,
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("service"), "smartfetch-browser-worker")
        self.assertIn("active_jobs", data)
        self.assertIn("capacity", data)

    def test_02_missing_client_certificate_rejected(self) -> None:
        """Connection without mTLS client certificate must fail at TLS layer."""
        with self.assertRaises(requests.exceptions.RequestException):
            requests.get(
                f"{self.worker_url}/health",
                verify=self.ca_cert,
                timeout=5.0,
            )

    def test_03_unauthenticated_request_rejected(self) -> None:
        """POST without HMAC signature must be rejected with HTTP 401."""
        res = requests.post(
            f"{self.worker_url}/v1/render",
            data=b'{"url":"https://example.com"}',
            headers={"Content-Type": "application/json"},
            verify=self.ca_cert,
            cert=(self.client_cert, self.client_key),
            timeout=5.0,
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("error_code"), "invalid_request")

    def test_04_ssrf_destination_policy_blocked(self) -> None:
        """Targeting cloud metadata or private IP must be rejected with policy_rejection."""
        ssrf_targets = [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8080/internal",
            "http://10.0.0.1/admin",
        ]
        for target in ssrf_targets:
            with self.subTest(target=target):
                with self.assertRaises(RemoteBrowserError) as cm:
                    self.client.render(target)
                self.assertEqual(cm.exception.code, ERROR_POLICY_REJECTION)

    def test_05_live_rendering_real_webpage(self) -> None:
        """Isolated worker must successfully render a real webpage via Chromium sandbox."""
        res = self.client.render("https://example.com")
        self.assertTrue(res.get("success"), f"Render failed: {res}")
        self.assertEqual(res.get("status_code"), 200)
        self.assertIn("example.com", res.get("final_url", ""))
        self.assertIn("Example Domain", res.get("title", ""))
        self.assertEqual(res.get("render_method"), "browser")
        self.assertTrue(len(res.get("html", "")) > 100)
        self.assertTrue(len(res.get("content", "")) > 50)

    def test_06_circuit_breaker_tripping_fail_closed(self) -> None:
        """Repeated infrastructure failures must trip circuit breaker to OPEN."""
        unreachable_config = RemoteBrowserConfig(
            worker_url="https://127.0.0.1:1",
            hmac_key="dummy_key",
            failure_threshold=2,
            connect_timeout=0.3,
        )
        breaker_client = RemoteBrowserClient(unreachable_config)

        # Force threshold failures
        for _ in range(2):
            try:
                breaker_client.render("https://example.com")
            except RemoteBrowserError:
                pass

        self.assertTrue(breaker_client.circuit_breaker.is_open)
        with self.assertRaises(RemoteBrowserError) as cm:
            breaker_client.render("https://example.com")
        self.assertEqual(cm.exception.code, ERROR_BROWSER_UNAVAILABLE)
        self.assertIn("circuit breaker is OPEN", cm.exception.message)


if __name__ == "__main__":
    unittest.main()
