import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from x402.extensions.bazaar import validate_discovery_extension_spec
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import bazaar, server
from smartfetch.payments import BASE_SEPOLIA, X402Settings


REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
PAID_SETTINGS = X402Settings(
    enabled=True,
    pay_to=VALID_ADDRESS,
    price='$0.005',
    network=BASE_SEPOLIA,
)
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme='exact',
    network=BASE_SEPOLIA,
)])
MCP_HEADERS = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}


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
        'clientInfo': {'name': 'smartfetch-v18-test', 'version': '1.0'},
    })


def call_fetch(client):
    return rpc(client, 'tools/call', {
        'name': 'fetch_webpage',
        'arguments': {
            'url': 'https://example.com/',
            'max_chars': 20000,
            'force_browser': False,
        },
    }, request_id=3)


class V18MCPBazaarMetadataTests(unittest.TestCase):
    def test_mcp_declaration_describes_the_existing_tool_contract(self):
        self.assertTrue(
            hasattr(bazaar, 'fetch_mcp_discovery_extension'),
            'V1.8 must expose an MCP-specific Bazaar declaration',
        )

        extension = bazaar.fetch_mcp_discovery_extension()
        declaration = extension['bazaar']
        self.assertTrue(
            validate_discovery_extension_spec(declaration).valid,
        )
        self.assertEqual(declaration['info']['input'], {
            'type': 'mcp',
            'toolName': 'fetch_webpage',
            'description': bazaar.FETCH_DESCRIPTION,
            'transport': 'streamable-http',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'url': {
                        'type': 'string',
                        'format': 'uri',
                        'pattern': '^https?://',
                        'description': 'Public HTTP or HTTPS URL to retrieve.',
                    },
                    'max_chars': {
                        'type': 'integer',
                        'minimum': 1000,
                        'maximum': 50000,
                        'default': 20000,
                        'description': (
                            'Maximum characters returned for content and '
                            'Markdown.'
                        ),
                    },
                    'force_browser': {
                        'type': 'boolean',
                        'default': False,
                        'description': (
                            'Start with browser rendering instead of HTTP '
                            'retrieval.'
                        ),
                    },
                },
                'required': ['url'],
            },
            'example': {
                'url': 'https://example.com/article',
                'max_chars': 20000,
                'force_browser': False,
            },
        })

        output = declaration['info']['output']
        self.assertEqual(output['type'], 'json')
        self.assertTrue(output['example']['success'])
        self.assertEqual(output['example']['service_version'], '1.11.1')
        output_schema = declaration['schema']['properties']['output'][
            'properties'
        ]['example']
        Draft202012Validator(output_schema).validate(output['example'])
        self.assertEqual(
            hashlib.sha256(json.dumps(
                output_schema,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')).hexdigest(),
            '1242309f47ddf83e2056d4a4eceb16e2a90a7b10f2240cf4bb70b046d89eed6d',
        )


class V18MCPBazaarIntegrationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mcp_server_never_constructs_a_payment_wrapper(self):
        with patch(
            'x402.mcp.create_payment_wrapper',
            side_effect=AssertionError('x402 wrapper entered'),
        ) as wrapper:
            with TestClient(server.create_app(PAID_SETTINGS)) as client:
                initialize(client)
                result = call_fetch(client)
        wrapper.assert_not_called()
        self.assertTrue(result['result']['isError'])
        self.assertEqual(
            result['result']['structuredContent']['error_code'],
            'retrieval_unavailable',
        )

    def test_unpaid_challenge_is_discoverable_without_fetching(self):
        app = server.create_app(PAID_SETTINGS)

        with TestClient(app, base_url='https://agent.example') as client:
            initialized = initialize(client)
            listed = rpc(client, 'tools/list', {}, request_id=2)
            mcp_response = call_fetch(client)
            free_responses = [
                client.get('/'),
                client.get('/health'),
                client.get('/meta'),
            ]
            http_response = client.post('/fetch', json={
                'url': 'https://example.com/article',
            })

        self.assertIn('result', initialized)
        self.assertEqual(
            [tool['name'] for tool in listed['result']['tools']],
            [
                'fetch_webpage',
                'webpage_to_markdown',
                'extract_webpage_text',
                'render_webpage',
            ],
        )
        for response in free_responses:
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('payment-required', response.headers)

        challenge = mcp_response['result']['structuredContent']
        self.assertTrue(mcp_response['result']['isError'])
        self.assertEqual(challenge['error_code'], 'retrieval_unavailable')
        self.assertNotIn('accepts', json.dumps(mcp_response))
        self.assertEqual(http_response.status_code, 503)
        self.assertEqual(http_response.json()['error_code'], 'retrieval_unavailable')
        self.assertNotIn('payment-required', http_response.headers)


class V18RegistryManifestTests(unittest.TestCase):
    def test_manifest_is_remote_only_and_contains_no_payment_secrets(self):
        manifest = json.loads((REPO_ROOT / 'server.json').read_text(
            encoding='utf-8'
        ))

        self.assertEqual(manifest, {
            '$schema': (
                'https://static.modelcontextprotocol.io/schemas/'
                '2025-12-11/server.schema.json'
            ),
            'name': 'io.github.Friezaaaa/smartfetch',
            'title': 'SmartFetch',
            'description': (
                'Emergency fail-closed service: retrieval and browser '
                'rendering are temporarily unavailable; no payment challenge is active.'
            ),
            'version': '1.11.1',
            'repository': {
                'url': 'https://github.com/Friezaaaa/smartfetch',
                'source': 'github',
            },
            'remotes': [{
                'type': 'streamable-http',
                'url': (
                    'https://smartfetch-production-ea53.up.railway.app/mcp'
                ),
            }],
        })
        serialized = json.dumps(manifest).lower()
        for forbidden in (
            'packages',
            'headers',
            'api_key',
            'private_key',
            'wallet',
            'x402_pay_to',
            'cdp_api',
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == '__main__':
    unittest.main()
