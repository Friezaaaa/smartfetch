"""Integration tests for remote browser client wiring in smartfetch.browser_fetch and core."""

import os
import unittest
from unittest.mock import MagicMock, patch

from smartfetch.browser_fetch import browser_fetch
from smartfetch.core import smart_fetch
from smartfetch.diagnostics import RetrievalFailure, RetrievalDiagnostics
from smartfetch.remote_browser import (
    ERROR_BROWSER_UNAVAILABLE,
    ERROR_POLICY_REJECTION,
    ERROR_RENDER_FAILED,
    ERROR_TIMEOUT,
    RemoteBrowserConfig,
    RemoteBrowserError,
)


class RemoteBrowserIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._orig_env = os.environ.copy()
        os.environ["BROWSER_WORKER_URL"] = "https://worker.smartfetch.internal:8443"
        os.environ["BROWSER_WORKER_HMAC_KEY"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    @patch("smartfetch.browser_fetch.RemoteBrowserClient")
    def test_browser_fetch_dispatches_to_remote_worker_on_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.render.return_value = {
            "success": True,
            "requested_url": "https://example.com",
            "final_url": "https://example.com/done",
            "status_code": 200,
            "render_method": "browser",
            "html": "<html><body><h1>Hello VPS</h1></body></html>",
            "title": "Hello VPS",
            "content": "Hello VPS",
            "markdown": "# Hello VPS",
        }

        result = browser_fetch("https://example.com")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["final_url"], "https://example.com/done")
        self.assertEqual(result["title"], "Hello VPS")
        self.assertIn("Hello VPS", result["html"])
        mock_client.render.assert_called_once()

    @patch("smartfetch.browser_fetch.RemoteBrowserClient")
    def test_browser_fetch_annotates_diagnostics_on_remote_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.render.side_effect = RemoteBrowserError(
            ERROR_TIMEOUT,
            "Render timed out on worker",
            url="https://example.com",
            phase="browser_navigate",
        )

        with self.assertRaises(Exception) as ctx:
            browser_fetch("https://example.com")

        exc = ctx.exception
        diag = getattr(exc, "retrieval_diagnostics", None)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.phase, "browser_navigate")
        self.assertEqual(diag.failure_code, "timeout")

    @patch("smartfetch.browser_fetch.RemoteBrowserClient")
    def test_smart_fetch_propagates_remote_browser_diagnostics(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.render.side_effect = RemoteBrowserError(
            ERROR_POLICY_REJECTION,
            "Destination blocked by policy",
            url="https://example.com",
            phase="validate",
        )

        with self.assertRaises(RetrievalFailure) as ctx:
            smart_fetch("https://example.com", force_browser=True)

        diag = ctx.exception.retrieval_diagnostics
        self.assertIsInstance(diag, RetrievalDiagnostics)
        self.assertEqual(diag.phase, "validate")
        self.assertEqual(diag.failure_code, "policy_rejection")

    def test_browser_fetch_fails_closed_when_worker_required_and_unconfigured(self):
        os.environ.pop("BROWSER_WORKER_URL", None)
        os.environ.pop("BROWSER_WORKER_HMAC_KEY", None)
        os.environ["BROWSER_WORKER_REQUIRED"] = "true"

        with self.assertRaises(Exception) as ctx:
            browser_fetch("https://example.com")

        exc = ctx.exception
        diag = getattr(exc, "retrieval_diagnostics", None)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.failure_code, "browser_failure")
        self.assertIn("VPS browser worker is required", str(exc))


if __name__ == "__main__":
    unittest.main()
