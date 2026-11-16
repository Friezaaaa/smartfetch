"""Tests for v1.13 CORS preflight support and Phase 1 Playground single-file application."""

from pathlib import Path
import unittest
from starlette.testclient import TestClient

from smartfetch.server import create_app
from smartfetch.payments import X402Settings


FREE_SETTINGS = X402Settings(
    enabled=False,
    pay_to=None,
    price="$0.005",
    network="eip155:8453",
)


class TestCORSPreflightAndHeaders(unittest.TestCase):
    def setUp(self):
        self.app = create_app(FREE_SETTINGS)

    def test_options_preflight_on_fetch_and_mcp(self):
        """OPTIONS /fetch and OPTIONS /mcp return 200 OK with Access-Control-Allow-Origin: * and allow PAYMENT-SIGNATURE / Accept."""
        with TestClient(self.app) as client:
            # 1. OPTIONS /fetch preflight
            r_fetch = client.options(
                "/fetch",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type, PAYMENT-SIGNATURE",
                },
            )
            self.assertEqual(r_fetch.status_code, 200)
            self.assertEqual(r_fetch.headers.get("access-control-allow-origin"), "*")
            allow_headers = r_fetch.headers.get("access-control-allow-headers", "").lower()
            self.assertIn("payment-signature", allow_headers)
            self.assertIn("content-type", allow_headers)

            # 2. OPTIONS /mcp preflight
            r_mcp = client.options(
                "/mcp",
                headers={
                    "Origin": "https://glama.ai",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type, Accept",
                },
            )
            self.assertEqual(r_mcp.status_code, 200)
            self.assertEqual(r_mcp.headers.get("access-control-allow-origin"), "*")
            allow_headers_mcp = r_mcp.headers.get("access-control-allow-headers", "").lower()
            self.assertIn("accept", allow_headers_mcp)

    def test_post_fetch_exposes_payment_headers(self):
        """POST /fetch responses include Access-Control-Expose-Headers with PAYMENT-REQUIRED and PAYMENT-RESPONSE."""
        with TestClient(self.app) as client:
            r = client.post(
                "/fetch",
                json={"url": "https://example.com"},
                headers={"Origin": "http://localhost:3000"},
            )
            # Response must have CORS origin header
            self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
            expose = r.headers.get("access-control-expose-headers", "")
            self.assertIn("PAYMENT-REQUIRED", expose)
            self.assertIn("PAYMENT-RESPONSE", expose)
            self.assertIn("X-Request-Id", expose)

    def test_non_post_non_options_methods_return_405(self):
        """GET, PUT, DELETE, PATCH on /fetch and /mcp continue returning 405 Method Not Allowed."""
        with TestClient(self.app) as client:
            r_get_fetch = client.get("/fetch", headers={"Origin": "http://localhost:3000"})
            self.assertEqual(r_get_fetch.status_code, 405)
            self.assertEqual(r_get_fetch.headers.get("access-control-allow-origin"), "*")

            r_put_fetch = client.put("/fetch", json={}, headers={"Origin": "http://localhost:3000"})
            self.assertEqual(r_put_fetch.status_code, 405)

            r_del_mcp = client.delete("/mcp", headers={"Origin": "https://glama.ai"})
            self.assertEqual(r_del_mcp.status_code, 405)


class TestPlaygroundSandbox(unittest.TestCase):
    def test_playground_file_exists_and_strictly_sandboxed(self):
        """examples/playground/index.html exists and strictly uses sandbox='allow-scripts' without allow-same-origin."""
        repo_root = Path(__file__).resolve().parent.parent
        playground_path = repo_root / "examples" / "playground" / "index.html"

        self.assertTrue(playground_path.is_file(), f"Playground file not found at {playground_path}")
        content = playground_path.read_text(encoding="utf-8")

        # Iframe must have sandbox="allow-scripts"
        self.assertIn('sandbox="allow-scripts"', content)

        # Must strictly omit dangerous capabilities
        self.assertNotIn("allow-same-origin", content)
        self.assertNotIn("allow-top-navigation", content)


if __name__ == "__main__":
    unittest.main()
