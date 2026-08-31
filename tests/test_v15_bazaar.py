import hashlib
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from x402.extensions.bazaar import (
    bazaar_resource_server_extension,
    validate_discovery_extension,
    validate_discovery_extension_spec,
)
from x402.http.middleware import fastapi as x402_fastapi_middleware
from x402.http.utils import decode_payment_required_header
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import server
from smartfetch.bazaar import fetch_discovery_extension
from smartfetch.payments import X402Settings


VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
BASE_SEPOLIA = 'eip155:84532'
PAID_SETTINGS = X402Settings(
    True,
    VALID_ADDRESS,
    '$0.005',
    BASE_SEPOLIA,
)
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme='exact',
    network=BASE_SEPOLIA,
)])


class V16BazaarDiscoveryTests(unittest.TestCase):
    def test_static_post_fetch_declaration_is_valid_before_installation(self):
        bazaar = fetch_discovery_extension()['bazaar']

        result = validate_discovery_extension(bazaar)

        self.assertTrue(result.valid, result.errors)

    def test_input_schema_rejects_non_object_json_bodies(self):
        bazaar = fetch_discovery_extension()['bazaar']
        body_schema = bazaar['schema']['properties']['input'][
            'properties'
        ]['body']

        errors = list(Draft202012Validator(body_schema).iter_errors([
            'not',
            'an',
            'object',
        ]))

        self.assertEqual(body_schema.get('type'), 'object')
        self.assertTrue(errors)

    def test_output_example_is_internally_consistent_and_http_eligible(self):
        bazaar = fetch_discovery_extension()['bazaar']
        example = bazaar['info']['output']['example']
        content = example['content']
        markdown = example['markdown']

        self.assertGreaterEqual(len(content), 80)
        self.assertFalse(example['low_quality'])
        self.assertEqual(example['word_count'], len(content.split()))
        self.assertEqual(
            example['content_hash'],
            hashlib.sha256(content.encode('utf-8')).hexdigest(),
        )
        self.assertEqual(example['original_content_chars'], len(content))
        self.assertEqual(example['returned_content_chars'], len(content))
        self.assertEqual(example['original_markdown_chars'], len(markdown))
        self.assertEqual(example['returned_markdown_chars'], len(markdown))
        self.assertEqual(example['links_returned'], len(example['links']))

    @patch(
        'x402.http.HTTPFacilitatorClient.get_supported',
        return_value=SUPPORTED,
    )
    def test_paid_fetch_402_contains_valid_agent_ready_bazaar_metadata(
        self, _get_supported
    ):
        with TestClient(
            server.create_app(PAID_SETTINGS),
            base_url='https://agent.example',
        ) as client:
            response = client.post('/fetch', json={
                'url': 'https://example.com/article',
                'max_chars': 20000,
                'force_browser': False,
            })

        self.assertEqual(response.status_code, 402)
        payment_required = decode_payment_required_header(
            response.headers['payment-required']
        )
        extensions = payment_required.extensions or {}
        self.assertIn('bazaar', extensions)
        bazaar = extensions['bazaar']

        self.assertTrue(validate_discovery_extension_spec(bazaar).valid)
        self.assertTrue(validate_discovery_extension(bazaar).valid)
        self.assertEqual(
            str(payment_required.resource.url),
            'https://agent.example/fetch',
        )
        self.assertEqual(payment_required.resource.service_name, 'SmartFetch')
        self.assertEqual(
            payment_required.resource.description,
            'Read, fetch, scrape, or extract any public webpage or URL for '
            'AI agents. Returns clean text, Markdown, links, and metadata, '
            'with automatic browser rendering for JavaScript-heavy pages.',
        )
        self.assertEqual(
            payment_required.resource.tags,
            ['web-reader', 'web-scraping', 'markdown', 'browser', 'agents'],
        )
        self.assertLessEqual(len(payment_required.resource.description), 500)

        input_info = bazaar['info']['input']
        self.assertEqual(input_info, {
            'type': 'http',
            'method': 'POST',
            'bodyType': 'json',
            'body': {
                'url': 'https://example.com/article',
                'max_chars': 20000,
                'force_browser': False,
            },
        })
        body_schema = bazaar['schema']['properties']['input'][
            'properties'
        ]['body']
        self.assertEqual(body_schema['required'], ['url'])
        self.assertNotIn('additionalProperties', body_schema)
        self.assertEqual(body_schema['properties']['url']['type'], 'string')
        self.assertEqual(
            body_schema['properties']['max_chars']['type'], 'integer'
        )
        self.assertEqual(
            body_schema['properties']['force_browser']['type'], 'boolean'
        )
        self.assertEqual(
            hashlib.sha256(json.dumps(
                body_schema,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')).hexdigest(),
            '6f08e7d505fc6f40e0f964662830b0c8a8f5e7c25f1240d9a7ee500f716ef84d',
        )

        output_example = bazaar['info']['output']['example']
        self.assertTrue(output_example['success'])
        self.assertEqual(output_example['render_method'], 'http')
        self.assertEqual(output_example['service_version'], '1.10.5')
        self.assertTrue({
            'requested_url',
            'final_url',
            'status_code',
            'title',
            'content',
            'markdown',
            'links',
            'word_count',
            'content_hash',
            'truncated',
            'elapsed_ms',
            'request_id',
        }.issubset(output_example))
        output_schema = bazaar['schema']['properties']['output'][
            'properties'
        ]['example']
        self.assertEqual(
            hashlib.sha256(json.dumps(
                output_schema,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')).hexdigest(),
            '1242309f47ddf83e2056d4a4eceb16e2a90a7b10f2240cf4bb70b046d89eed6d',
        )

        accepted = payment_required.accepts[0]
        self.assertEqual(accepted.scheme, 'exact')
        self.assertEqual(accepted.network, BASE_SEPOLIA)
        self.assertEqual(accepted.pay_to, VALID_ADDRESS)
        self.assertEqual(accepted.amount, '5000')

        serialized = json.dumps(bazaar, sort_keys=True)
        for forbidden in (
            'CDP_API_KEY_ID',
            'CDP_API_KEY_SECRET',
            'https://x402.org/facilitator',
            'AppData',
            VALID_ADDRESS,
        ):
            self.assertNotIn(forbidden, serialized)

    @patch(
        'x402.http.HTTPFacilitatorClient.get_supported',
        return_value=SUPPORTED,
    )
    def test_bazaar_extension_is_registered_before_middleware_installation(
        self, _get_supported
    ):
        observed_extensions = []
        real_payment_middleware = x402_fastapi_middleware.payment_middleware

        def inspect_registration(*, routes, server, **kwargs):
            observed_extensions.append(server._extensions.get('bazaar'))
            return real_payment_middleware(
                routes=routes,
                server=server,
                **kwargs,
            )

        with patch.object(
            x402_fastapi_middleware,
            'payment_middleware',
            side_effect=inspect_registration,
        ):
            app = server.create_app(PAID_SETTINGS)

        self.assertEqual(
            observed_extensions,
            [bazaar_resource_server_extension],
        )

    @patch(
        'x402.http.HTTPFacilitatorClient.get_supported',
        return_value=SUPPORTED,
    )
    def test_only_post_fetch_is_paid_and_discoverable(self, _get_supported):
        with TestClient(server.create_app(PAID_SETTINGS)) as client:
            for path in ('/', '/health', '/meta'):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn('payment-required', response.headers)

            get_fetch = client.get('/fetch')
            paid_fetch = client.post('/fetch', json={
                'url': 'https://example.com/article',
            })

        self.assertEqual(get_fetch.status_code, 405)
        self.assertNotIn('payment-required', get_fetch.headers)
        self.assertEqual(paid_fetch.status_code, 402)
        decoded = decode_payment_required_header(
            paid_fetch.headers['payment-required']
        )
        self.assertIn('bazaar', decoded.extensions or {})


if __name__ == '__main__':
    unittest.main()
