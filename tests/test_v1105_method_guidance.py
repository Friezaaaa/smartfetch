import unittest
from unittest.mock import patch
import socket

from fastapi.testclient import TestClient
import x402.http.middleware.fastapi as official_http
import x402.server as official_server

from smartfetch import server
from smartfetch.mcp_server import MCP_TOOLS
from smartfetch.payments import X402Settings


FREE_SETTINGS = X402Settings(False, None, '$0.005', 'eip155:84532')
PAID_SETTINGS = X402Settings(
    True,
    '0x1111111111111111111111111111111111111111',
    '$0.005',
    'eip155:84532',
)
EXPECTED_GUIDANCE = {
    'error': 'method_not_allowed',
    'message': 'Use POST /fetch.',
    'openapi': '/openapi.json',
    'documentation': '/docs',
    'payment_discovery': '/.well-known/x402',
}


class FetchMethodGuidanceTests(unittest.TestCase):
    def test_get_fetch_returns_free_machine_readable_guidance(self):
        with TestClient(server.create_app(FREE_SETTINGS)) as client:
            response = client.get('/fetch')

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers['allow'], 'POST')
        self.assertEqual(response.headers['content-type'], 'application/json')
        guidance = response.json()
        for key, value in EXPECTED_GUIDANCE.items():
            self.assertEqual(guidance[key], value)
        self.assertIs(guidance['retrieval_available'], False)
        self.assertEqual(guidance['mode'], 'emergency_fail_closed')
        self.assertEqual(response.headers['x-smartfetch-availability'], 'unavailable')

    def test_head_fetch_returns_free_empty_guidance_response(self):
        with TestClient(server.create_app(FREE_SETTINGS)) as client:
            response = client.head('/fetch')

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers['allow'], 'POST')
        self.assertEqual(response.headers['x-smartfetch-availability'], 'unavailable')
        self.assertEqual(response.content, b'')

    def test_openapi_still_exposes_only_post_fetch(self):
        with TestClient(server.create_app(FREE_SETTINGS)) as client:
            response = client.get('/openapi.json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()['paths']['/fetch']), {'post'})

    def test_configured_payment_cannot_challenge_guidance_or_retrieval(self):
        with (
            patch.object(official_http, 'payment_middleware', side_effect=AssertionError('payment wrapper entered')),
            patch.object(official_server.x402ResourceServer, 'verify_payment', side_effect=AssertionError('verification entered')),
            patch.object(official_server.x402ResourceServer, 'settle_payment', side_effect=AssertionError('settlement entered')),
            patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS entered')),
            TestClient(server.create_app(PAID_SETTINGS)) as client,
        ):
            get_response = client.get('/fetch')
            head_response = client.head('/fetch')
            post_response = client.post(
                '/fetch',
                json={'url': 'https://example.com/'},
            )
            meta_response = client.get('/meta')

        self.assertEqual(get_response.status_code, 405)
        for key, value in EXPECTED_GUIDANCE.items():
            self.assertEqual(get_response.json()[key], value)
        self.assertIs(get_response.json()['retrieval_available'], False)
        self.assertEqual(head_response.status_code, 405)
        self.assertEqual(head_response.content, b'')
        self.assertEqual(post_response.status_code, 503)
        self.assertEqual(post_response.json()['error_code'], 'retrieval_unavailable')
        self.assertEqual(post_response.headers['cache-control'], 'no-store')
        self.assertNotIn('payment-required', post_response.headers)
        self.assertEqual([tool['name'] for tool in meta_response.json()['tools']], list(MCP_TOOLS))
        self.assertIs(meta_response.json()['retrieval_available'], False)


if __name__ == '__main__':
    unittest.main()
