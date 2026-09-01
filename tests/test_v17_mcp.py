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
        self.assertEqual(tool['description'], FETCH_DESCRIPTION)
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
        self.assertEqual(slash_response.status_code, 404)

    def test_meta_advertises_exact_streamable_http_route_and_tool(self):
        with TestClient(server.create_app(X402Settings(
            False,
            None,
            '$0.005',
            BASE_SEPOLIA,
        ))) as client:
            meta = client.get('/meta').json()

        self.assertEqual(meta['version'], '1.10.6')
        self.assertEqual(meta['mcp'], {
            'enabled': True,
            'path': '/mcp',
            'transport': 'streamable-http',
            'tool': 'fetch_webpage',
            'tools': [
                'fetch_webpage',
                'webpage_to_markdown',
                'extract_webpage_text',
                'render_webpage',
            ],
            'url': 'http://testserver/mcp',
        })


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

    def test_unpaid_call_challenges_without_fetching(self):
        app, _get_supported = self._paid_app()

        with (
            patch('smartfetch.server._run_fetch', new_callable=AsyncMock) as fetch,
            TestClient(app) as client,
        ):
            initialize(client)
            listed = rpc(client, 'tools/list', {}, request_id=2)
            challenge_response = call_fetch(client)

        fetch.assert_not_awaited()
        self.assertEqual(len(listed['result']['tools']), 4)
        result = challenge_response['result']
        self.assertTrue(result['isError'])
        challenge = result['structuredContent']
        self.assertEqual(challenge['x402Version'], 2)
        self.assertEqual(challenge['error'], 'Payment Required')
        self.assertEqual(challenge['resource'], {
            'url': 'mcp://tool/fetch_webpage',
            'description': FETCH_DESCRIPTION,
            'mimeType': 'application/json',
            'serviceName': 'SmartFetch',
        })
        self.assertEqual(len(challenge['accepts']), 1)
        accepted = challenge['accepts'][0]
        self.assertEqual(accepted['scheme'], 'exact')
        self.assertEqual(accepted['network'], BASE_SEPOLIA)
        self.assertEqual(accepted['payTo'], VALID_ADDRESS)
        self.assertEqual(accepted['amount'], '5000')

    def test_valid_mocked_payment_fetches_once_and_settles_once(self):
        app, _get_supported = self._paid_app()
        mcp_payment = app.state.smartfetch_mcp
        payment_requirement = mcp_payment.accepts[0]
        resource_server = mcp_payment.resource_server
        resource_server.find_matching_requirements = Mock(
            return_value=payment_requirement,
        )
        resource_server.verify_payment = AsyncMock(return_value=VerifyResponse(
            isValid=True,
            payer=VALID_ADDRESS,
        ))
        resource_server.settle_payment = AsyncMock(return_value=SettleResponse(
            success=True,
            payer=VALID_ADDRESS,
            transaction='0xabc123',
            network=BASE_SEPOLIA,
        ))
        payment = {
            'x402Version': 2,
            'accepted': payment_requirement.model_dump(
                by_alias=True,
                exclude_none=True,
            ),
            'payload': {'mockAuthorization': 'public-test-value'},
        }
        fetch_result = {
            'success': True,
            'requested_url': 'https://example.com/',
            'final_url': 'https://example.com/',
            'status_code': 200,
            'render_method': 'http',
            'title': 'Example Domain',
            'content': 'Example content',
            'markdown': 'Example content',
            'links': [],
            'word_count': 2,
            'content_hash': '0' * 64,
            'low_quality': False,
            'truncated': False,
            'elapsed_ms': 10,
            'max_chars': 20000,
        }

        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                return_value=fetch_result,
            ) as fetch,
            TestClient(app) as client,
        ):
            response = call_fetch(client, payment=payment)

        fetch.assert_awaited_once_with('https://example.com/', False, 20000)
        resource_server.verify_payment.assert_awaited_once()
        resource_server.settle_payment.assert_awaited_once()
        result = response['result']
        self.assertFalse(result['isError'])
        body = json.loads(result['content'][0]['text'])
        self.assertTrue(body['success'])
        self.assertEqual(body['service_version'], '1.10.6')
        self.assertRegex(body['request_id'], '^[0-9a-f]{16}$')
        settlement = result['_meta']['x402/payment-response']
        self.assertTrue(settlement['success'])
        self.assertEqual(settlement['transaction'], '0xabc123')
        self.assertEqual(settlement['network'], BASE_SEPOLIA)

    def test_sepolia_and_mainnet_keep_existing_payment_configuration(self):
        for network in (BASE_SEPOLIA, BASE_MAINNET):
            with self.subTest(network=network):
                app, _get_supported = self._paid_app(network)
                with TestClient(app) as client:
                    challenge = call_fetch(client)['result'][
                        'structuredContent'
                    ]
                accepted = challenge['accepts'][0]
                self.assertEqual(accepted['scheme'], 'exact')
                self.assertEqual(accepted['network'], network)
                self.assertEqual(accepted['payTo'], VALID_ADDRESS)
                self.assertEqual(accepted['amount'], '5000')

    def test_only_tool_execution_and_existing_post_fetch_are_paid(self):
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
        self.assertEqual(http_challenge.status_code, 402)
        decoded = decode_payment_required_header(
            http_challenge.headers['payment-required']
        )
        self.assertIn('bazaar', decoded.extensions or {})
        self.assertEqual(str(decoded.resource.url), 'https://agent.example/fetch')

    def test_server_mcp_layer_has_no_wallet_or_private_key_input(self):
        source = inspect.getsource(mcp_server).lower()
        self.assertNotIn('private_key', source)
        self.assertNotIn('wallet_secret', source)
        signature = inspect.signature(mcp_server.create_smartfetch_mcp)
        self.assertNotIn('private_key', signature.parameters)
        self.assertNotIn('wallet_secret', signature.parameters)

    def test_mcp_initialization_failure_is_closed_and_redacts_secrets(self):
        settings = paid_settings(BASE_MAINNET)
        stdout = io.StringIO()
        stderr = io.StringIO()

        async def unused_fetch(_url, _force_browser, _max_chars):
            raise AssertionError('fetch must not run during initialization')

        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            patch(
                'smartfetch.mcp_server.create_x402_resource_server',
                side_effect=RuntimeError(CDP_KEY_SECRET),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                '^x402 MCP initialization failed$',
            ) as caught,
        ):
            mcp_server.create_smartfetch_mcp(settings, unused_fetch)

        exposed = ''.join(traceback.format_exception(caught.exception))
        exposed += stdout.getvalue() + stderr.getvalue()
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(CDP_KEY_ID, exposed)
        self.assertNotIn(CDP_KEY_SECRET, exposed)


if __name__ == '__main__':
    unittest.main()
