import hashlib
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from x402.extensions.bazaar import validate_discovery_extension, validate_discovery_extension_spec
from x402.http.middleware import fastapi as x402_fastapi_middleware

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

    def test_static_bazaar_declaration_remains_valid_but_is_not_publicly_payable(self):
        with TestClient(
            server.create_app(PAID_SETTINGS),
            base_url='https://agent.example',
        ) as client:
            response = client.post('/fetch', json={
                'url': 'https://example.com/article',
                'max_chars': 20000,
                'force_browser': False,
            })
            manifest = client.get('/.well-known/x402').json()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error_code'], 'retrieval_unavailable')
        self.assertNotIn('payment-required', response.headers)
        self.assertEqual(manifest['resources'], [])
        bazaar = fetch_discovery_extension()['bazaar']

        self.assertTrue(validate_discovery_extension_spec(bazaar).valid)
        self.assertTrue(validate_discovery_extension(bazaar).valid)
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
        # This declaration is retained for compatibility but is not advertised
        # while retrieval is unavailable.
        self.assertEqual(output_example['service_version'], '1.11.1')
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

        serialized = json.dumps(bazaar, sort_keys=True)
        for forbidden in (
            'CDP_API_KEY_ID',
            'CDP_API_KEY_SECRET',
            'https://x402.org/facilitator',
            'AppData',
            VALID_ADDRESS,
        ):
            self.assertNotIn(forbidden, serialized)

    def test_emergency_app_never_registers_bazaar_payment_middleware(self):
        with patch.object(
            x402_fastapi_middleware, 'payment_middleware',
            side_effect=AssertionError('x402 middleware entered'),
        ) as middleware:
            with TestClient(server.create_app(PAID_SETTINGS)) as client:
                response = client.post('/fetch', json={
                    'url': 'https://example.com/article',
                })
        self.assertEqual(response.status_code, 503)
        middleware.assert_not_called()

    def test_free_discovery_and_post_fetch_is_unavailable_not_payable(self):
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
        self.assertEqual(paid_fetch.status_code, 503)
        self.assertEqual(paid_fetch.json()['error_code'], 'retrieval_unavailable')
        self.assertNotIn('payment-required', paid_fetch.headers)


if __name__ == '__main__':
    unittest.main()
