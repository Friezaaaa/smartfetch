import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from x402.http.utils import decode_payment_required_header
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch.payments import X402Settings
from smartfetch import server


FREE_SETTINGS = X402Settings(False, None, '$0.005', 'eip155:84532')


class FastAPICompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.app = server.create_app(FREE_SETTINGS)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def assert_v12_headers(self, response):
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(
            response.headers['x-request-id'], response.json()['request_id']
        )

    def test_health_and_metadata_keep_existing_shapes(self):
        health = self.client.get('/health')
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()['ok'])
        self.assertEqual(health.json()['service'], 'SmartFetch')
        self.assertEqual(health.json()['version'], '1.5.0')
        self.assertIn('uptime_seconds', health.json())
        self.assert_v12_headers(health)

        for path in ('/', '/meta'):
            with self.subTest(path=path):
                meta = self.client.get(path)
                self.assertEqual(meta.status_code, 200)
                self.assertEqual(meta.json()['version'], '1.5.0')
                self.assertEqual(
                    meta.json()['endpoint'], {'method': 'POST', 'path': '/fetch'}
                )
                self.assertEqual(meta.json()['payment'], 'not-enabled-yet')
                self.assert_v12_headers(meta)

    def test_unknown_routes_methods_and_generated_docs_are_not_exposed(self):
        for method, path in (
            ('get', '/missing'),
            ('post', '/missing'),
            ('get', '/fetch'),
            ('post', '/health'),
            ('get', '/docs'),
            ('get', '/redoc'),
            ('get', '/openapi.json'),
            ('put', '/fetch'),
            ('patch', '/fetch'),
            ('delete', '/fetch'),
            ('options', '/fetch'),
            ('head', '/fetch'),
            ('trace', '/fetch'),
            ('connect', '/fetch'),
            ('propfind', '/fetch'),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.request(method.upper(), path)
                self.assertEqual(response.status_code, 404)
                if method == 'head':
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual(
                        response.headers['x-content-type-options'], 'nosniff'
                    )
                    self.assertTrue(response.headers['x-request-id'])
                else:
                    body = response.json()
                    self.assertEqual(body['error_code'], 'not_found')
                    self.assert_v12_headers(response)

    def test_invalid_fetch_bodies_keep_existing_error_shapes(self):
        cases = (
            ({'content': b'', 'headers': {'Content-Length': '0'}}, 'invalid_body_size'),
            ({'content': b'{', 'headers': {'Content-Type': 'application/json'}}, 'invalid_json'),
            ({'json': ['not', 'an', 'object']}, 'invalid_request'),
            ({'json': {}}, 'invalid_request'),
            ({'json': {'url': '   '}}, 'invalid_request'),
        )
        for request_kwargs, error_code in cases:
            with self.subTest(error_code=error_code, request=request_kwargs):
                response = self.client.post('/fetch', **request_kwargs)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error_code'], error_code)
                self.assert_v12_headers(response)

        with patch.object(server, 'MAX_REQUEST_BODY_BYTES', 3):
            oversized = self.client.post('/fetch', content=b'{}  ')
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(oversized.json()['error_code'], 'invalid_body_size')
        self.assert_v12_headers(oversized)

    @patch('smartfetch.server._rate_allowed', return_value=True)
    @patch('smartfetch.server.smart_fetch')
    def test_free_fetch_preserves_success_augmentation(self, smart_fetch, _rate):
        smart_fetch.return_value = {
            'success': True,
            'status_code': 200,
            'content': 'retrieved',
        }

        response = self.client.post('/fetch', json={
            'url': 'https://example.com/article',
            'force_browser': True,
            'max_chars': 1234,
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['service_version'], '1.5.0')
        self.assert_v12_headers(response)
        smart_fetch.assert_called_once_with(
            'https://example.com/article', True, 1234
        )

    @patch('smartfetch.server._rate_allowed', return_value=True)
    @patch('smartfetch.server.smart_fetch', side_effect=ValueError('blocked'))
    def test_blocked_target_keeps_existing_error_mapping(self, _fetch, _rate):
        response = self.client.post('/fetch', json={'url': 'http://127.0.0.1'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['error_code'], 'invalid_or_blocked_target'
        )
        self.assertEqual(response.json()['error'], 'blocked')
        self.assert_v12_headers(response)

    @patch('smartfetch.server._rate_allowed', return_value=False)
    def test_rate_limit_keeps_existing_error_and_retry_header(self, _rate):
        response = self.client.post('/fetch', json={'url': 'https://example.com'})

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['error_code'], 'rate_limited')
        self.assertEqual(response.headers['retry-after'], '60')
        self.assert_v12_headers(response)

    @patch('smartfetch.server._rate_allowed', return_value=True)
    @patch('smartfetch.server._FETCH_SLOTS')
    def test_capacity_limit_keeps_existing_error_and_retry_header(
        self, slots, _rate
    ):
        slots.acquire.return_value = False

        response = self.client.post('/fetch', json={'url': 'https://example.com'})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error_code'], 'busy')
        self.assertEqual(response.headers['retry-after'], '2')
        self.assert_v12_headers(response)
        slots.release.assert_not_called()

    @patch('smartfetch.server._rate_allowed', return_value=True)
    @patch('smartfetch.server._run_fetch', new_callable=AsyncMock)
    def test_timeout_keeps_existing_error_mapping(self, run_fetch, _rate):
        run_fetch.side_effect = asyncio.TimeoutError()

        response = self.client.post('/fetch', json={'url': 'https://example.com'})

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()['error_code'], 'fetch_timeout')
        self.assert_v12_headers(response)

    @patch('smartfetch.server._rate_allowed', return_value=True)
    @patch('smartfetch.server.smart_fetch', side_effect=RuntimeError('upstream'))
    def test_fetch_failure_keeps_existing_error_mapping(self, _fetch, _rate):
        response = self.client.post('/fetch', json={'url': 'https://example.com'})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error_code'], 'fetch_failed')
        self.assertEqual(response.json()['error'], 'upstream')
        self.assert_v12_headers(response)

    @patch('x402.http.HTTPFacilitatorClient.get_supported')
    def test_integrated_unpaid_response_has_matching_request_id(
        self, get_supported
    ):
        get_supported.return_value = SupportedResponse(kinds=[SupportedKind(
            x402Version=2,
            scheme='exact',
            network='eip155:84532',
        )])
        paid_settings = X402Settings(
            True,
            '0x1111111111111111111111111111111111111111',
            '$0.005',
            'eip155:84532',
        )

        with TestClient(server.create_app(paid_settings)) as client:
            response = client.post('/fetch', json={'url': 'https://example.com'})
            health = client.get('/health')

        self.assertEqual(response.status_code, 402)
        self.assertEqual(
            response.json()['request_id'], response.headers['x-request-id']
        )
        self.assertEqual(health.status_code, 200)


class UvicornProxyConfigurationTests(unittest.TestCase):
    def decoded_resource_url(self, environ):
        supported = SupportedResponse(kinds=[SupportedKind(
            x402Version=2,
            scheme='exact',
            network='eip155:84532',
        )])
        settings = X402Settings(
            True,
            '0x1111111111111111111111111111111111111111',
            '$0.005',
            'eip155:84532',
        )
        test_environ = dict(os.environ)
        test_environ.pop('FORWARDED_ALLOW_IPS', None)
        test_environ.pop('RAILWAY_ENVIRONMENT_ID', None)
        test_environ.update(environ)
        with (
            patch.dict(os.environ, test_environ, clear=True),
            patch(
                'x402.http.HTTPFacilitatorClient.get_supported',
                return_value=supported,
            ),
        ):
            config = server.create_uvicorn_config(server.create_app(settings))
            config.load()
            with TestClient(
                config.loaded_app,
                base_url='http://test-host.example',
            ) as client:
                response = client.post(
                    '/fetch',
                    json={'url': 'https://example.com'},
                    headers={
                        'Host': 'test-host.example',
                        'X-Forwarded-For': '203.0.113.10',
                        'X-Forwarded-Proto': 'https',
                    },
                )

        self.assertEqual(response.status_code, 402)
        payment_required = decode_payment_required_header(
            response.headers['payment-required']
        )
        return str(payment_required.resource.url)

    def test_railway_forwarded_https_is_advertised_by_x402(self):
        resource_url = self.decoded_resource_url({
            'RAILWAY_ENVIRONMENT_ID': 'test-environment-id',
        })

        self.assertEqual(resource_url, 'https://test-host.example/fetch')

    def test_explicit_forwarded_allow_ips_overrides_railway_default(self):
        resource_url = self.decoded_resource_url({
            'RAILWAY_ENVIRONMENT_ID': 'test-environment-id',
            'FORWARDED_ALLOW_IPS': '127.0.0.1',
        })

        self.assertEqual(resource_url, 'http://test-host.example/fetch')

    def test_explicit_forwarded_allow_ips_is_honored_outside_railway(self):
        resource_url = self.decoded_resource_url({
            'FORWARDED_ALLOW_IPS': '*',
        })

        self.assertEqual(resource_url, 'https://test-host.example/fetch')

    def test_non_railway_keeps_uvicorn_localhost_proxy_default(self):
        resource_url = self.decoded_resource_url({})

        self.assertEqual(resource_url, 'http://test-host.example/fetch')


if __name__ == '__main__':
    unittest.main()
