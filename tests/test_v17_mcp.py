import contextlib
import inspect
import io
import json
import traceback
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from x402.http.utils import decode_payment_required_header
from x402.schemas import (
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

from smartfetch import mcp_server, server
from smartfetch.bazaar import FETCH_DESCRIPTION
from smartfetch.payments import BASE_MAINNET, BASE_SEPOLIA, X402Settings


VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
CDP_KEY_ID = 'organizations/test/apiKeys/test'
CDP_KEY_SECRET = (
    '-----BEGIN EC PRIVATE KEY-----\n'
    'MHcCAQEEIFakeTestMaterialForMockedInitializationOnly1234567890\n'
    '-----END EC PRIVATE KEY-----\n'
)
MCP_HEADERS = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}


def paid_settings(network=BASE_SEPOLIA):
    return X402Settings(
        enabled=True,
        pay_to=VALID_ADDRESS,
        price='$0.005',
        network=network,
        cdp_api_key_id=(CDP_KEY_ID if network == BASE_MAINNET else None),
        cdp_api_key_secret=(
            CDP_KEY_SECRET if network == BASE_MAINNET else None
        ),
    )


def supported(network):
    return SupportedResponse(kinds=[SupportedKind(
        x402Version=2,
        scheme='exact',
        network=network,
    )])


def rpc(client, method, params=None, request_id=1):
    payload = {
        'jsonrpc': '2.0',
        'id': request_id,
        'method': method,
    }
    if params is not None:
        payload['params'] = params
    response = client.post('/mcp', headers=MCP_HEADERS, json=payload)
    if response.status_code != 200:
        raise AssertionError(
            f'MCP {method} returned {response.status_code}: {response.text}'
        )
    return response.json()


def initialize(client):
    return rpc(client, 'initialize', {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'smartfetch-test', 'version': '1.0'},
    })


def call_fetch(client, *, payment=None):
    params = {
        'name': 'fetch_webpage',
        'arguments': {
            'url': 'https://example.com/',
            'max_chars': 20000,
            'force_browser': False,
        },
    }
    if payment is not None:
        params['_meta'] = {'x402/payment': payment}
    return rpc(client, 'tools/call', params, request_id=3)


class V17MCPDiscoveryTests(unittest.TestCase):
    def test_route_initializes_and_lists_exactly_four_free_tools(self):
        app = server.create_app(X402Settings(
            False,
            None,
            '$0.005',
            BASE_SEPOLIA,
        ))

        with TestClient(app, follow_redirects=False) as client:
            initialized = initialize(client)
            listed = rpc(client, 'tools/list', {}, request_id=2)
            slash_response = client.post(
                '/mcp/',
                headers=MCP_HEADERS,
                json={'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'},
            )

        self.assertEqual(initialized['result']['serverInfo']['name'], 'SmartFetch')
        self.assertEqual(initialized['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(
            [item['name'] for item in listed['result']['tools']],
            [
                'fetch_webpage',
                'webpage_to_markdown',
                'extract_webpage_text',
                'render_webpage',
            ],
        )
        tool = listed['result']['tools'][0]
        self.assertEqual(tool['name'], 'fetch_webpage')
        self.assertIn('unavailable', tool['description'].lower())
        self.assertEqual(tool['_meta']['availability'], 'unavailable')
        self.assertFalse(tool['_meta']['payment_challenge_enabled'])
        self.assertEqual(tool['inputSchema'], {
            'properties': {
                'url': {'title': 'Url', 'type': 'string'},
                'max_chars': {
                    'default': 20000,
                    'maximum': 50000,
                    'minimum': 1000,
                    'title': 'Max Chars',
                    'type': 'integer',
                },
                'force_browser': {
                    'default': False,
                    'title': 'Force Browser',
                    'type': 'boolean',
                },
            },
            'required': ['url'],
            'title': 'fetch_webpageArguments',
            'type': 'object',
        })
        self.assertEqual(slash_response.status_code, 307)
        self.assertEqual(slash_response.headers['location'], 'http://testserver/mcp')
        self.assertNotIn('payment-required', slash_response.headers)

    def test_meta_advertises_exact_streamable_http_route_and_tool(self):
        with TestClient(server.create_app(X402Settings(
            False,
            None,
            '$0.005',
            BASE_SEPOLIA,
        ))) as client:
            meta = client.get('/meta').json()

        self.assertEqual(meta['version'], '1.11.1')
        self.assertEqual(meta['mcp']['url'], 'http://testserver/mcp')
        self.assertEqual([tool['name'] for tool in meta['tools']], [
            'fetch_webpage', 'webpage_to_markdown',
            'extract_webpage_text', 'render_webpage',
        ])
        self.assertTrue(all(tool['availability'] == 'unavailable'
                            for tool in meta['tools']))


class V17MCPPaymentTests(unittest.TestCase):
    def _paid_app(self, network=BASE_SEPOLIA):
        capability = supported(network)
        patcher = patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=capability,
        )
        get_supported = patcher.start()
        self.addCleanup(patcher.stop)
        return server.create_app(paid_settings(network)), get_supported

    def test_unpaid_call_is_unavailable_without_payment_or_fetching(self):
        app, get_supported = self._paid_app()

        with TestClient(app) as client:
            initialize(client)
            listed = rpc(client, 'tools/list', {}, request_id=2)
            unavailable_response = call_fetch(client)

        get_supported.assert_not_called()
        self.assertEqual(len(listed['result']['tools']), 4)
        result = unavailable_response['result']
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['error_code'],
                         'retrieval_unavailable')
        self.assertNotIn('accepts', json.dumps(result))

    def test_payment_payload_never_verifies_executes_or_settles(self):
        app, get_supported = self._paid_app()
        payment = {'mockAuthorization': 'private-proof-canary'}
        with (
            patch('x402.server.x402ResourceServer.verify_payment',
                  side_effect=AssertionError('verify entered')) as verify,
            patch('x402.server.x402ResourceServer.settle_payment',
                  side_effect=AssertionError('settle entered')) as settle,
            TestClient(app) as client,
        ):
            response = call_fetch(client, payment=payment)
        result = response['result']
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['error_code'],
                         'retrieval_unavailable')
        self.assertNotIn('private-proof-canary', json.dumps(result))
        self.assertNotIn('payment-response', json.dumps(result).lower())
        get_supported.assert_not_called()
        verify.assert_not_called()
        settle.assert_not_called()

    def test_sepolia_and_mainnet_settings_cannot_enable_payment(self):
        for network in (BASE_SEPOLIA, BASE_MAINNET):
            with self.subTest(network=network):
                app, get_supported = self._paid_app(network)
                with TestClient(app) as client:
                    unavailable = call_fetch(client)['result'][
                        'structuredContent'
                    ]
                self.assertEqual(unavailable['error_code'], 'retrieval_unavailable')
                self.assertNotIn('accepts', unavailable)
                get_supported.assert_not_called()

    def test_free_lifecycle_and_unavailable_http_and_mcp_execution(self):
        app, _get_supported = self._paid_app()

        with TestClient(app, base_url='https://agent.example') as client:
            for path in ('/', '/health', '/meta'):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn('payment-required', response.headers)
            initialize_response = initialize(client)
            list_response = rpc(client, 'tools/list', {}, request_id=2)
            mcp_challenge = call_fetch(client)
            http_challenge = client.post('/fetch', json={
                'url': 'https://example.com/',
            })

        self.assertIn('result', initialize_response)
        self.assertEqual(len(list_response['result']['tools']), 4)
        self.assertTrue(mcp_challenge['result']['isError'])
        self.assertEqual(mcp_challenge['result']['structuredContent']['error_code'],
                         'retrieval_unavailable')
        self.assertEqual(http_challenge.status_code, 503)
        self.assertEqual(http_challenge.json()['error_code'], 'retrieval_unavailable')
        self.assertNotIn('payment-required', http_challenge.headers)

    def test_server_mcp_layer_has_no_wallet_or_private_key_input(self):
        source = inspect.getsource(mcp_server).lower()
        self.assertNotIn('private_key', source)
        self.assertNotIn('wallet_secret', source)
        signature = inspect.signature(mcp_server.create_smartfetch_mcp)
        self.assertNotIn('private_key', signature.parameters)
        self.assertNotIn('wallet_secret', signature.parameters)

    def test_mcp_initialization_does_not_construct_payment_server_or_expose_secrets(self):
        settings = paid_settings(BASE_MAINNET)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            patch(
                'smartfetch.payments.create_x402_resource_server',
                side_effect=RuntimeError(CDP_KEY_SECRET),
            ) as resource,
        ):
            with TestClient(server.create_app(settings)) as client:
                initialized = initialize(client)
                listed = rpc(client, 'tools/list', {}, request_id=2)

        self.assertIn('result', initialized)
        self.assertEqual(len(listed['result']['tools']), 4)
        resource.assert_not_called()
        exposed = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(CDP_KEY_ID, exposed)
        self.assertNotIn(CDP_KEY_SECRET, exposed)


if __name__ == '__main__':
    unittest.main()
