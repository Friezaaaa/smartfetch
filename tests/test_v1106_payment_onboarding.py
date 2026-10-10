import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from x402.http.utils import (
    decode_payment_required_header,
    encode_payment_signature_header,
)
from x402.schemas import (
    PaymentPayload,
    ResourceVerifyResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

from smartfetch import discovery, payments, server
from smartfetch.config import SERVICE_VERSION
from smartfetch.mcp_server import MCP_TOOLS
from smartfetch.payments import BASE_MAINNET, X402Settings


REPO_ROOT = Path(__file__).resolve().parents[1]
PAYEE = '0x1111111111111111111111111111111111111111'
BASE_MAINNET_USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
PRICE = '$0.0075'
SECRET_SAFETY = (
    'Never place a private key or recovery phrase in a URL, request body, '
    'log, example, command-line argument, or repository file. Never commit '
    'secret-bearing .env files. Use hidden interactive input, a '
    'platform-injected secret, or an approved wallet/secret-management '
    'service.'
)


def paid_settings():
    return X402Settings(
        True,
        PAYEE,
        PRICE,
        BASE_MAINNET,
        'organizations/test/apiKeys/test',
        'test-only-cdp-secret',
    )


def create_paid_app():
    facilitator = Mock()
    facilitator.get_supported.return_value = SupportedResponse(kinds=[
        SupportedKind(
            x402Version=2,
            scheme='exact',
            network=BASE_MAINNET,
        ),
    ])
    with patch(
        'smartfetch.payments.create_facilitator',
        return_value=facilitator,
    ):
        return server.create_app(paid_settings())


class PaymentOnboardingOpenAPITests(unittest.TestCase):
    def test_public_openapi_matches_real_unavailable_response(self):
        app = create_paid_app()
        with TestClient(app, base_url='https://agent.example') as client:
            document = client.get('/openapi.json').json()
            response = client.post('/fetch', json={
                'url': 'https://example.com/',
            })
        operation = document['paths']['/fetch']['post']
        self.assertEqual(document['x-smartfetch-mode'], 'emergency_fail_closed')
        self.assertEqual(operation['x-smartfetch-availability'], 'unavailable')
        self.assertTrue(operation['requestBody']['required'])
        self.assertIn('application/json', operation['requestBody']['content'])
        self.assertIn('503', operation['responses'])
        self.assertNotIn('402', operation['responses'])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error_code'], 'retrieval_unavailable')
        self.assertNotIn('payment-required', response.headers)
        self.assertNotIn('payment-response', response.headers)

    def test_openapi_advertises_no_actionable_x402_headers(self):
        app = create_paid_app()
        with TestClient(app) as client:
            operation = client.get('/openapi.json').json()['paths']['/fetch']['post']

        self.assertFalse(operation['payment_challenge_enabled'])
        serialized = json.dumps(operation).lower()
        for forbidden in ('payment-signature', 'payment-required',
                          'payment-response', 'x-x402', 'accepts'):
            self.assertNotIn(forbidden, serialized)

    def test_openapi_has_finite_503_and_validation_responses(self):
        app = create_paid_app()
        with TestClient(app) as client:
            responses = client.get('/openapi.json').json()[
                'paths']['/fetch']['post']['responses']

        self.assertEqual(set(responses), {'503', '400', '415', '422'})
        self.assertEqual(responses['503']['description'],
                         'Retrieval is temporarily unavailable')

    def test_payment_header_never_enters_verification_or_settlement(self):
        with (
            patch('smartfetch.payments.create_x402_resource_server',
                  side_effect=AssertionError('payment server entered')) as resource,
            patch('x402.server.x402ResourceServer.verify_payment',
                  side_effect=AssertionError('verification entered')) as verify,
            patch('x402.server.x402ResourceServer.settle_payment',
                  side_effect=AssertionError('settlement entered')) as settle,
            TestClient(server.create_app(paid_settings())) as client,
        ):
            response = client.post('/fetch',
                                   headers={'PAYMENT-SIGNATURE': 'opaque-test-authorization'},
                                   json={'url': 'https://example.com/'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error_code'], 'retrieval_unavailable')
        self.assertNotIn('payment-response', response.headers)
        resource.assert_not_called()
        verify.assert_not_called()
        settle.assert_not_called()

class PaymentOnboardingDocumentationTests(unittest.TestCase):
    def test_docs_and_llms_disclose_unavailable_not_payment_requirement(self):
        app = create_paid_app()
        with TestClient(app, base_url='https://agent.example') as client:
            docs = client.get('/docs').text
            llms = client.get('/llms.txt').text

        for output in (docs, llms):
            with self.subTest(output=output[:20]):
                self.assertIn('unavailable', output.lower())
                self.assertNotIn(PRICE, output)
                self.assertNotIn(BASE_MAINNET, output)
                self.assertNotIn(BASE_MAINNET_USDC, output)

    def test_emergency_docs_are_independent_of_configured_price(self):
        urls = {
            key: f'https://agent.example/{path}'
            for key, path in {
                'base': '',
                'x402': '.well-known/x402',
                'docs': 'docs',
                'openapi': 'openapi.json',
                'llms': 'llms.txt',
                'robots': 'robots.txt',
                'sitemap': 'sitemap.xml',
                'meta': 'meta',
                'fetch': 'fetch',
                'mcp': 'mcp',
            }.items()
        }

        docs = discovery.emergency_docs_html(urls)
        llms = discovery.emergency_llms_text(urls)

        for output in (docs, llms):
            self.assertIn('unavailable', output.lower())
            self.assertNotIn('$0.00875', output)
            self.assertNotIn(PRICE, output)

    def test_docs_do_not_invite_payment_for_unavailable_retrieval(self):
        app = create_paid_app()
        with TestClient(app, base_url='https://agent.example') as client:
            docs = client.get('/docs').text

        self.assertIn('temporarily unavailable', docs)
        self.assertIn('Do not submit a payment', docs)
        for forbidden in ('PAYMENT-REQUIRED', 'PAYMENT-SIGNATURE',
                          'PAYMENT-RESPONSE', 'paid_fetch_mainnet_test.py',
                          'quickstart-for-buyers'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, docs)

    def test_llms_is_bounded_and_uses_markdown_navigation_links(self):
        app = create_paid_app()
        with TestClient(app, base_url='https://agent.example') as client:
            llms = client.get('/llms.txt').text

        self.assertLess(len(llms), 4000)
        self.assertIn(
            '- [OpenAPI 3.1](https://agent.example/openapi.json)',
            llms,
        )
        self.assertIn('- [Documentation](https://agent.example/docs)', llms)
        self.assertNotIn('paid_fetch_mainnet_test.py', llms)
        self.assertNotIn('```', llms)

    def test_version_and_registry_manifest_are_emergency_v1111(self):
        manifest = json.loads(
            (REPO_ROOT / 'server.json').read_text(encoding='utf-8')
        )

        self.assertEqual(SERVICE_VERSION, '1.11.1')
        self.assertEqual(manifest['version'], '1.11.1')
        self.assertEqual(MCP_TOOLS, (
            'fetch_webpage',
            'webpage_to_markdown',
            'extract_webpage_text',
            'render_webpage',
        ))


if __name__ == '__main__':
    unittest.main()
