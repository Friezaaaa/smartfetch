"""Tests for the Railway-side remote browser client contract and fail-closed behaviors."""

from __future__ import annotations

import json
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from smartfetch.remote_browser import (
    CircuitBreakerState,
    ERROR_BROWSER_UNAVAILABLE,
    ERROR_INVALID_REQUEST,
    ERROR_POLICY_REJECTION,
    ERROR_RENDER_FAILED,
    ERROR_TIMEOUT,
    RemoteBrowserClient,
    RemoteBrowserConfig,
    RemoteBrowserError,
    compute_canonical_string,
    sign_request,
    verify_signature,
)


class TestRemoteBrowserConfig(unittest.TestCase):
    def test_unconfigured_by_default(self):
        config = RemoteBrowserConfig.from_env({})
        self.assertFalse(config.is_configured)
        self.assertIsNone(config.worker_url)
        self.assertIsNone(config.hmac_key)

    def test_configured_from_env(self):
        env = {
            "BROWSER_WORKER_URL": "https://worker.internal:8443",
            "BROWSER_WORKER_HMAC_KEY": "supersecretkey32byteslongexactly!",
            "BROWSER_WORKER_CONNECT_TIMEOUT": "5.0",
            "BROWSER_WORKER_RENDER_TIMEOUT": "20.0",
        }
        config = RemoteBrowserConfig.from_env(env)
        self.assertTrue(config.is_configured)
        self.assertEqual(config.worker_url, "https://worker.internal:8443")
        self.assertEqual(config.hmac_key, "supersecretkey32byteslongexactly!")
        self.assertEqual(config.connect_timeout, 5.0)
        self.assertEqual(config.render_timeout, 20.0)


class TestAuthAndSignatures(unittest.TestCase):
    def setUp(self):
        self.key = "test-hmac-key-0123456789abcdef"
        self.body = b'{"url":"https://example.com"}'

    def test_signature_generation_and_verification(self):
        headers = sign_request(
            "POST",
            "/v1/render",
            self.body,
            self.key,
            timestamp=1700000000,
            nonce="0123456789abcdef0123456789abcdef",
            request_id="test-req-1234",
        )
        self.assertIn("X-SmartFetch-Signature", headers)
        self.assertEqual(headers["X-SmartFetch-Timestamp"], "1700000000")
        self.assertEqual(headers["X-SmartFetch-Nonce"], "0123456789abcdef0123456789abcdef")
        self.assertEqual(headers["X-SmartFetch-Request-Id"], "test-req-1234")

        # Verify signature with matched timestamp within skew window
        with patch("time.time", return_value=1700000010):
            valid = verify_signature(
                "POST",
                "/v1/render",
                self.body,
                self.key,
                headers,
                max_skew_seconds=30,
            )
            self.assertTrue(valid)

    def test_signature_tamper_detection(self):
        headers = sign_request("POST", "/v1/render", self.body, self.key, timestamp=1700000000)
        # Tampered body
        with patch("time.time", return_value=1700000005):
            valid = verify_signature("POST", "/v1/render", b"tampered", self.key, headers)
            self.assertFalse(valid)

        # Tampered path
        with patch("time.time", return_value=1700000005):
            valid = verify_signature("GET", "/v1/render", self.body, self.key, headers)
            self.assertFalse(valid)

    def test_signature_timestamp_skew_rejection(self):
        headers = sign_request("POST", "/v1/render", self.body, self.key, timestamp=1700000000)
        # 31 seconds later (skew > 30)
        with patch("time.time", return_value=1700000035):
            valid = verify_signature("POST", "/v1/render", self.body, self.key, headers, max_skew_seconds=30)
            self.assertFalse(valid)


class TestCircuitBreaker(unittest.TestCase):
    def test_circuit_state_transitions(self):
        cb = CircuitBreakerState(failure_threshold=2, recovery_timeout=0.2)
        self.assertEqual(cb.current_state, "CLOSED")
        self.assertFalse(cb.is_open)

        # 1st failure
        cb.record_failure()
        self.assertEqual(cb.current_state, "CLOSED")
        self.assertFalse(cb.is_open)

        # 2nd failure -> Tripped to OPEN
        cb.record_failure()
        self.assertEqual(cb.current_state, "OPEN")
        self.assertTrue(cb.is_open)

        # Still open before recovery timeout
        time.sleep(0.05)
        self.assertTrue(cb.is_open)

        # After recovery timeout -> HALF_OPEN
        time.sleep(0.2)
        self.assertFalse(cb.is_open)  # allows trial probe
        self.assertEqual(cb.current_state, "HALF_OPEN")

        # Success in HALF_OPEN resets to CLOSED
        cb.record_success()
        self.assertEqual(cb.current_state, "CLOSED")
        self.assertFalse(cb.is_open)


class TestRemoteBrowserClient(unittest.TestCase):
    def setUp(self):
        self.config = RemoteBrowserConfig(
            worker_url="https://worker.internal:8443",
            hmac_key="secret-key-32-bytes-minimum-here!",
            connect_timeout=2.0,
            render_timeout=5.0,
            failure_threshold=2,
            recovery_timeout=0.5,
        )
        self.mock_session = MagicMock(spec=requests.Session)

    def test_unconfigured_client_fails_closed(self):
        client = RemoteBrowserClient(RemoteBrowserConfig())
        self.assertFalse(client.is_available)
        with self.assertRaises(RemoteBrowserError) as ctx:
            client.render("https://example.com")
        self.assertEqual(ctx.exception.code, ERROR_BROWSER_UNAVAILABLE)
        self.assertEqual(ctx.exception.phase, "validate")

    def test_open_circuit_breaker_fails_closed(self):
        client = RemoteBrowserClient(self.config, session=self.mock_session)
        client.circuit_breaker.record_failure()
        client.circuit_breaker.record_failure()
        self.assertTrue(client.circuit_breaker.is_open)

        with self.assertRaises(RemoteBrowserError) as ctx:
            client.render("https://example.com")
        self.assertEqual(ctx.exception.code, ERROR_BROWSER_UNAVAILABLE)
        self.assertEqual(ctx.exception.phase, "capacity")

    def test_invalid_url_policy_rejection(self):
        client = RemoteBrowserClient(self.config, session=self.mock_session)
        # Loopback URL is blocked by validate_public_url
        with self.assertRaises(RemoteBrowserError) as ctx:
            client.render("http://127.0.0.1:8080/test")
        self.assertEqual(ctx.exception.code, ERROR_POLICY_REJECTION)
        self.assertIn("policy", ctx.exception.message.lower())

    def test_successful_render(self):
        client = RemoteBrowserClient(self.config, session=self.mock_session)
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "requested_url": "https://example.com",
            "final_url": "https://example.com/",
            "status_code": 200,
            "render_method": "browser",
            "content": "Rendered text",
            "html": "<html><body>Rendered text</body></html>",
            "markdown": "Rendered text",
        }
        self.mock_session.post.return_value = mock_response

        # Use public safe URL
        result = client.render("https://example.com")
        self.assertTrue(result["success"])
        self.assertEqual(result["final_url"], "https://example.com/")
        self.assertEqual(result["render_method"], "browser")
        self.assertEqual(result["content"], "Rendered text")
        self.assertIn("elapsed_ms", result)

    def test_remote_worker_error_mapping(self):
        client = RemoteBrowserClient(self.config, session=self.mock_session)
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 403
        mock_response.json.return_value = {
            "success": False,
            "error_code": ERROR_POLICY_REJECTION,
            "error": "Target host denied by broker destination policy",
            "phase": "connect",
        }
        self.mock_session.post.return_value = mock_response

        with self.assertRaises(RemoteBrowserError) as ctx:
            client.render("https://example.com")
        self.assertEqual(ctx.exception.code, ERROR_POLICY_REJECTION)
        self.assertEqual(ctx.exception.phase, "connect")

    def test_timeout_trips_circuit_breaker(self):
        client = RemoteBrowserClient(self.config, session=self.mock_session)
        self.mock_session.post.side_effect = requests.exceptions.Timeout("Connection timed out")

        # 1st timeout
        with self.assertRaises(RemoteBrowserError) as ctx:
            client.render("https://example.com")
        self.assertEqual(ctx.exception.code, ERROR_TIMEOUT)
        self.assertEqual(client.circuit_breaker.current_state, "CLOSED")

        # 2nd timeout -> Trips circuit breaker to OPEN
        with self.assertRaises(RemoteBrowserError) as ctx:
            client.render("https://example.com")
        self.assertEqual(ctx.exception.code, ERROR_TIMEOUT)
        self.assertEqual(client.circuit_breaker.current_state, "OPEN")

    def test_connection_error_handling(self):
        client = RemoteBrowserClient(self.config, session=self.mock_session)
        self.mock_session.post.side_effect = requests.exceptions.ConnectionError("Refused")

        with self.assertRaises(RemoteBrowserError) as ctx:
            client.render("https://example.com")
        self.assertEqual(ctx.exception.code, ERROR_BROWSER_UNAVAILABLE)
        self.assertEqual(ctx.exception.phase, "connect")


if __name__ == "__main__":
    unittest.main()
