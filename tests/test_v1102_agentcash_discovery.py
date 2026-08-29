import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import discovery, server
from smartfetch.config import SERVICE_VERSION
from smartfetch.mcp_server import MCP_TOOLS
from smartfetch.payments import BASE_MAINNET, X402Settings


REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
BASE_MAINNET_USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
SUPPORTED_MAINNET = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme='exact',
    network=BASE_MAINNET,
)])
URLS = {
    'base': 'https://agent.example',
    'x402': 'https://agent.example/.well-known/x402',
    'docs': 'https://agent.example/docs',
    'openapi': 'https://agent.example/openapi.json',
    'llms': 'https://agent.example/llms.txt',
    'robots': 'https://agent.example/robots.txt',
    'sitemap': 'https://agent.example/sitemap.xml',
    'meta': 'https://agent.example/meta',
    'fetch': 'https://agent.example/fetch',
    'mcp': 'https://agent.example/mcp',
}
GUIDANCE = (
    'Use POST /fetch as a paid fallback when ordinary access to a public '
    'HTTP(S) webpage fails or is incomplete, when JavaScript rendering is '
    'required, or when clean text or Markdown is needed. Send url and '
    'optional max_chars and force_browser. Do not use SmartFetch to bypass '
    'CAPTCHAs, authentication, paywalls, robots restrictions, or access '
    'controls.'
)


def paid_settings(price='$0.005'):
    return X402Settings(
        True,
        VALID_ADDRESS,
        price,
        BASE_MAINNET,
        'organizations/test/apiKeys/test',
        'never-expose-cdp-secret',
    )


def requirement(amount='5000'):
    return Mock(
        scheme='exact',
        network=BASE_MAINNET,
        asset=BASE_MAINNET_USDC,
        amount=amount,
    )


class AgentCashOpenAPITests(unittest.TestCase):
    def test_paid_fetch_has_canonical_agentcash_metadata(self):
        document = discovery.openapi_document(
            URLS,
            paid_settings(),
            requirement(),
        )

        self.assertEqual(document['info']['x-guidance'], GUIDANCE)
        operation = document['paths']['/fetch']['post']
        self.assertEqual(operation['x-payment-info'], {
            'price': {
                'mode': 'fixed',
                'currency': 'USD',
                'amount': '0.005000',
            },
            'protocols': [{'x402': {}}],
        })
        self.assertEqual(
            operation['responses']['402']['description'],
            'x402 v2 payment required',
        )
        self.assertEqual(operation['x-x402'], {
            'x402Version': 2,
            'scheme': 'exact',
            'network': BASE_MAINNET,
            'asset': BASE_MAINNET_USDC,
            'assetSymbol': 'USDC',
            'price': '$0.005',
            'amount': '5000',
        })

    def test_agentcash_price_is_derived_from_configured_price(self):
        document = discovery.openapi_document(
            URLS,
            paid_settings('$0.0075'),
            requirement('7500'),
        )
        operation = document['paths']['/fetch']['post']

        self.assertEqual(
            operation['x-payment-info']['price']['amount'],
            '0.007500',
        )
        self.assertEqual(operation['x-x402']['price'], '$0.0075')
        self.assertEqual(operation['x-x402']['amount'], '7500')

    def test_agentcash_metadata_matches_generated_mainnet_requirement(self):
        facilitator = Mock()
        facilitator.get_supported.return_value = SUPPORTED_MAINNET
        settings = paid_settings()
        with patch(
            'smartfetch.payments.create_facilitator',
            return_value=facilitator,
        ):
            app = server.create_app(settings)

        generated = app.state.smartfetch_mcp.accepts[0]
        with TestClient(app) as client:
            document = client.get('/openapi.json').json()

        operation = document['paths']['/fetch']['post']
        self.assertEqual(operation['x-x402']['scheme'], generated.scheme)
        self.assertEqual(operation['x-x402']['network'], generated.network)
        self.assertEqual(operation['x-x402']['asset'], generated.asset)
        self.assertEqual(operation['x-x402']['amount'], generated.amount)
        self.assertEqual(
            operation['x-payment-info']['price']['amount'],
            '0.005000',
        )

    def test_free_discovery_probes_never_execute_retrieval(self):
        facilitator = Mock()
        facilitator.get_supported.return_value = SUPPORTED_MAINNET
        retrieval = AsyncMock()
        with patch(
            'smartfetch.payments.create_facilitator',
            return_value=facilitator,
        ), patch('smartfetch.server._run_fetch', retrieval):
            app = server.create_app(paid_settings())
            with TestClient(app, base_url='https://agent.example') as client:
                responses = [
                    client.get('/openapi.json'),
                    client.get('/.well-known/x402'),
                    client.get('/meta'),
                ]

        self.assertTrue(all(
            response.status_code == 200 for response in responses
        ))
        self.assertTrue(all(
            'payment-required' not in response.headers
            for response in responses
        ))
        retrieval.assert_not_awaited()

    def test_discovery_metadata_exposes_no_payment_secrets_or_payee(self):
        serialized = json.dumps(discovery.openapi_document(
            URLS,
            paid_settings(),
            requirement(),
        )).lower()

        for forbidden in (
            VALID_ADDRESS.lower(),
            'never-expose-cdp-secret',
            'cdp_api_key',
            'private_key',
            'wallet_secret',
            'payment-signature',
        ):
            self.assertNotIn(forbidden, serialized)


class V1102VersionAndRegistryTests(unittest.TestCase):
    def test_service_and_registry_are_v1102_with_four_unchanged_tools(self):
        manifest = json.loads(
            (REPO_ROOT / 'server.json').read_text(encoding='utf-8')
        )

        self.assertEqual(SERVICE_VERSION, '1.10.2')
        self.assertEqual(manifest['version'], '1.10.2')
        self.assertEqual(
            manifest['description'],
            'Paid fallback for public webpages: clean text, Markdown, links, '
            'metadata, and JavaScript rendering.',
        )
        self.assertEqual(MCP_TOOLS, (
            'fetch_webpage',
            'webpage_to_markdown',
            'extract_webpage_text',
            'render_webpage',
        ))


if __name__ == '__main__':
    unittest.main()
