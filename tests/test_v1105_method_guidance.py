import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

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
        self.assertEqual(response.json(), EXPECTED_GUIDANCE)

    def test_head_fetch_returns_free_empty_guidance_response(self):
        with TestClient(server.create_app(FREE_SETTINGS)) as client:
            response = client.head('/fetch')

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers['allow'], 'POST')
        self.assertEqual(response.content, b'')

    def test_openapi_still_exposes_only_post_fetch(self):
        with TestClient(server.create_app(FREE_SETTINGS)) as client:
            response = client.get('/openapi.json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()['paths']['/fetch']), {'post'})

    @patch('x402.http.HTTPFacilitatorClient.verify', new_callable=AsyncMock)
    @patch('x402.http.HTTPFacilitatorClient.get_supported')
    @patch('smartfetch.server.smart_fetch')
    def test_paid_mode_guidance_is_free_and_post_remains_challenged(
        self,
        smart_fetch,
        get_supported,
        verify,
    ):
        get_supported.return_value = SupportedResponse(kinds=[SupportedKind(
            x402Version=2,
            scheme='exact',
            network='eip155:84532',
        )])

        with TestClient(server.create_app(PAID_SETTINGS)) as client:
            get_response = client.get('/fetch')
            head_response = client.head('/fetch')
            post_response = client.post(
                '/fetch',
                json={'url': 'https://example.com/'},
            )
            meta_response = client.get('/meta')

        self.assertEqual(get_response.status_code, 405)
        self.assertEqual(get_response.json(), EXPECTED_GUIDANCE)
        self.assertEqual(head_response.status_code, 405)
        self.assertEqual(head_response.content, b'')
        self.assertEqual(post_response.status_code, 402)
        self.assertEqual(meta_response.json()['mcp']['tools'], list(MCP_TOOLS))
        verify.assert_not_awaited()
        smart_fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
