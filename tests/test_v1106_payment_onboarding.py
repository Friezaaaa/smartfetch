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
    def test_402_header_example_matches_real_unpaid_challenge(self):
        app = create_paid_app()
        generated = app.state.smartfetch_mcp.accepts[0]

        with TestClient(app, base_url='https://agent.example') as client:
            document = client.get('/openapi.json').json()
            challenge_response = client.post('/fetch', json={
                'url': 'https://example.com/',
            })

        self.assertEqual(challenge_response.status_code, 402)
        self.assertEqual(
            set(challenge_response.json()),
            {'request_id'},
        )
        live = decode_payment_required_header(
            challenge_response.headers['payment-required']
        )

        response_402 = document['paths']['/fetch']['post']['responses']['402']
        self.assertIn('headers', response_402)
        self.assertIn('PAYMENT-REQUIRED', response_402['headers'])
        documented = decode_payment_required_header(
            response_402['headers']['PAYMENT-REQUIRED']['example']
        )
        body_contract = response_402['content']['application/json']

        self.assertEqual(body_contract['schema'], {
            'type': 'object',
            'properties': {'request_id': {'type': 'string'}},
            'required': ['request_id'],
            'additionalProperties': False,
        })
        self.assertEqual(body_contract['example'], {
            'request_id': 'a1b2c3d4e5f60708',
        })
        self.assertEqual(documented.x402_version, 2)
        self.assertEqual(documented.resource.url, 'https://agent.example/fetch')

        live_requirement = live.accepts[0]
        documented_requirement = documented.accepts[0]
        for field in ('scheme', 'network', 'amount', 'asset', 'pay_to'):
            with self.subTest(field=field):
                expected = getattr(generated, field)
                self.assertEqual(getattr(live_requirement, field), expected)
                self.assertEqual(getattr(documented_requirement, field), expected)

    def test_openapi_places_all_three_x402_headers_correctly(self):
        app = create_paid_app()
        with TestClient(app) as client:
            operation = client.get('/openapi.json').json()['paths']['/fetch']['post']

        self.assertIn('parameters', operation)
        signature = next(
            parameter for parameter in operation['parameters']
            if parameter['name'] == 'PAYMENT-SIGNATURE'
        )
        self.assertEqual(signature['in'], 'header')
        self.assertFalse(signature['required'])
        self.assertEqual(signature['schema'], {'type': 'string'})
        self.assertIn(
            'PAYMENT-REQUIRED',
            operation['responses']['402']['headers'],
        )
        self.assertIn(
            'PAYMENT-RESPONSE',
            operation['responses']['200']['headers'],
        )

    def test_openapi_has_realistic_502_example(self):
        app = create_paid_app()
        with TestClient(app) as client:
            response_502 = client.get('/openapi.json').json()[
                'paths'
            ]['/fetch']['post']['responses']['502']

        self.assertIn('example', response_502['content']['application/json'])
        self.assertEqual(
            response_502['content']['application/json']['example'],
            {
                'success': False,
                'error_code': 'fetch_failed',
                'error': 'Retrieval failed',
                'request_id': 'a1b2c3d4e5f60708',
            },
        )

    def test_verified_http_502_never_calls_settlement(self):
        facilitator = Mock()
        facilitator.get_supported.return_value = SupportedResponse(kinds=[
            SupportedKind(
                x402Version=2,
                scheme='exact',
                network=BASE_MAINNET,
            ),
        ])
        original_create = payments.create_x402_resource_server
        http_resource_servers = []

        def capture_http_resource_server(settings, *, register_bazaar=False):
            resource_server = original_create(
                settings,
                register_bazaar=register_bazaar,
            )
            http_resource_servers.append(resource_server)
            return resource_server

        with (
            patch(
                'smartfetch.payments.create_facilitator',
                return_value=facilitator,
            ),
            patch(
                'smartfetch.payments.create_x402_resource_server',
                side_effect=capture_http_resource_server,
            ),
        ):
            app = server.create_app(paid_settings())

        self.assertEqual(len(http_resource_servers), 1)
        resource_server = http_resource_servers[0]
        requirement = app.state.smartfetch_mcp.accepts[0]
        resource_server.verify_payment = AsyncMock(
            return_value=ResourceVerifyResponse(verify=VerifyResponse(
                isValid=True,
                payer='0x2222222222222222222222222222222222222222',
            )),
        )
        resource_server.settle_payment = AsyncMock()
        payment = PaymentPayload(
            accepted=requirement,
            payload={'authorization': 'opaque-test-authorization'},
        )

        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                side_effect=RuntimeError('test retrieval failure'),
            ),
            TestClient(app) as client,
        ):
            response = client.post(
                '/fetch',
                headers={
                    'PAYMENT-SIGNATURE': encode_payment_signature_header(
                        payment,
                    ),
                },
                json={'url': 'https://example.com/'},
            )

        self.assertEqual(
            response.status_code,
            502,
            decode_payment_required_header(
                response.headers['payment-required']
            ).error if response.status_code == 402 else response.text,
        )
        self.assertEqual(response.json()['error_code'], 'fetch_failed')
        resource_server.verify_payment.assert_awaited_once()
        resource_server.settle_payment.assert_not_awaited()

class PaymentOnboardingDocumentationTests(unittest.TestCase):
    def test_docs_and_llms_derive_active_payment_requirement(self):
        app = create_paid_app()
        with TestClient(app, base_url='https://agent.example') as client:
            docs = client.get('/docs').text
            llms = client.get('/llms.txt').text

        for output in (docs, llms):
            with self.subTest(output=output[:20]):
                self.assertIn('exact', output)
                self.assertIn(PRICE, output)
                self.assertIn(BASE_MAINNET, output)
                self.assertIn(BASE_MAINNET_USDC, output)

    def test_docs_price_comes_from_generated_atomic_requirement(self):
        app = create_paid_app()
        generated = app.state.smartfetch_mcp.accepts[0].model_copy(
            update={'amount': '8750'},
        )
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

        docs = discovery.docs_html(urls, paid_settings(), generated)
        llms = discovery.llms_text(urls, paid_settings(), generated)

        for output in (docs, llms):
            self.assertIn('$0.00875', output)
            self.assertNotIn(PRICE, output)

    def test_docs_explain_safe_http_payment_flow(self):
        app = create_paid_app()
        with TestClient(app, base_url='https://agent.example') as client:
            docs = client.get('/docs').text

        for required in (
            'PAYMENT-REQUIRED',
            'PAYMENT-SIGNATURE',
            'PAYMENT-RESPONSE',
            'scheme, network, asset, amount, and payee',
            'settles only after successful delivery',
            'status 400 or higher',
            SECRET_SAFETY,
            'scripts/paid_fetch_mainnet_test.py',
            'docs.x402.org/getting-started/quickstart-for-buyers',
        ):
            with self.subTest(required=required):
                self.assertIn(required, docs)

    def test_llms_is_bounded_and_uses_markdown_navigation_links(self):
        app = create_paid_app()
        with TestClient(app, base_url='https://agent.example') as client:
            llms = client.get('/llms.txt').text

        self.assertLess(len(llms), 4000)
        self.assertIn(
            '- [OpenAPI 3.1](https://agent.example/openapi.json)',
            llms,
        )
        self.assertIn(
            '- [HTTP buyer example]('
            'https://github.com/Friezaaaa/smartfetch/blob/main/'
            'scripts/paid_fetch_mainnet_test.py)',
            llms,
        )
        self.assertNotIn('```', llms)

    def test_version_and_registry_manifest_are_v1106(self):
        manifest = json.loads(
            (REPO_ROOT / 'server.json').read_text(encoding='utf-8')
        )

        self.assertEqual(SERVICE_VERSION, '1.10.6')
        self.assertEqual(manifest['version'], '1.10.6')
        self.assertEqual(MCP_TOOLS, (
            'fetch_webpage',
            'webpage_to_markdown',
            'extract_webpage_text',
            'render_webpage',
        ))


if __name__ == '__main__':
    unittest.main()
