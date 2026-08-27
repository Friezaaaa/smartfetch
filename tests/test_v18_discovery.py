import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from x402.extensions.bazaar import (
    bazaar_resource_server_extension,
    validate_discovery_extension_spec,
)
from x402.http.utils import decode_payment_required_header
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
        self.assertEqual(output['example']['service_version'], '1.9.0')
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

    def test_mcp_server_registers_and_passes_bazaar_to_payment_wrapper(self):
        from x402.mcp import create_payment_wrapper as real_wrapper

        with patch(
            'x402.mcp.create_payment_wrapper',
            wraps=real_wrapper,
        ) as wrapper:
            app = server.create_app(PAID_SETTINGS)

        mcp_payment = app.state.smartfetch_mcp
        self.assertIs(
            mcp_payment.resource_server._extensions['bazaar'],
            bazaar_resource_server_extension,
        )
        self.assertEqual(wrapper.call_count, 4)
        fetch_call = next(
            call for call in wrapper.call_args_list
            if str(call.kwargs['resource'].url) == 'mcp://tool/fetch_webpage'
        )
        extensions = fetch_call.kwargs['extensions']
        self.assertEqual(
            extensions['bazaar']['info']['input']['type'],
            'mcp',
        )
        self.assertEqual(
            extensions['bazaar']['info']['input']['toolName'],
            'fetch_webpage',
        )

    def test_unpaid_challenge_is_discoverable_without_fetching(self):
        app = server.create_app(PAID_SETTINGS)

        with (
            patch('smartfetch.server._run_fetch', new_callable=AsyncMock) as fetch,
            TestClient(app, base_url='https://agent.example') as client,
        ):
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

        fetch.assert_not_awaited()
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
        self.assertEqual(challenge['resource']['url'], 'mcp://tool/fetch_webpage')
        mcp_bazaar = challenge['extensions']['bazaar']
        self.assertTrue(validate_discovery_extension_spec(mcp_bazaar).valid)
        self.assertEqual(mcp_bazaar['info']['input']['type'], 'mcp')
        self.assertEqual(
            mcp_bazaar['info']['input']['toolName'],
            'fetch_webpage',
        )
        self.assertEqual(
            mcp_bazaar['info']['input']['transport'],
            'streamable-http',
        )
        accepted = challenge['accepts'][0]
        self.assertEqual(accepted['scheme'], 'exact')
        self.assertEqual(accepted['network'], BASE_SEPOLIA)
        self.assertEqual(accepted['payTo'], VALID_ADDRESS)
        self.assertEqual(accepted['amount'], '5000')

        self.assertEqual(http_response.status_code, 402)
        http_required = decode_payment_required_header(
            http_response.headers['payment-required']
        )
        http_bazaar = (http_required.extensions or {})['bazaar']
        self.assertEqual(http_bazaar['info']['input']['type'], 'http')
        self.assertEqual(http_bazaar['info']['input']['method'], 'POST')
        self.assertEqual(
            str(http_required.resource.url),
            'https://agent.example/fetch',
        )
        self.assertNotEqual(
            str(http_required.resource.url),
            challenge['resource']['url'],
        )


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
                'Read, fetch, scrape, and render public webpages into clean '
                'text, Markdown, links, and metadata.'
            ),
            'version': '1.9.0',
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
