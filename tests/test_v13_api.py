import os
import re
import socket
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
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
        self.assertRegex(response.headers['x-request-id'], r'^[0-9a-f]{16}$')

    def assert_emergency_unavailable(self, response, code='retrieval_unavailable'):
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error_code'], code)
        self.assertEqual(response.headers['retry-after'], '3600')
        self.assert_v12_headers(response)
        self.assertNotIn('payment-required', response.headers)
        self.assertNotIn('payment-response', response.headers)

    def test_health_and_metadata_keep_existing_shapes(self):
        health = self.client.get('/health')
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()['ok'])
        self.assertEqual(health.json()['service'], 'SmartFetch')
        self.assertEqual(health.json()['version'], '1.11.1')
        self.assertFalse(health.json()['retrieval_available'])
        self.assertFalse(health.json()['browser_available'])
        self.assertEqual(health.json()['mode'], 'emergency_fail_closed')
        self.assert_v12_headers(health)

        for path in ('/', '/meta'):
            with self.subTest(path=path):
                meta = self.client.get(path)
                self.assertEqual(meta.status_code, 200)
                self.assertIn('unavailable', meta.text.lower())
                self.assertNotIn('"accepts"', meta.text)
                self.assert_v12_headers(meta)

    def test_unknown_routes_methods_and_redoc_are_not_exposed(self):
        for method, path in (
            ('get', '/missing'),
            ('post', '/missing'),
            ('post', '/health'),
            ('get', '/redoc'),
            ('put', '/fetch'),
            ('patch', '/fetch'),
            ('delete', '/fetch'),
            ('options', '/fetch'),
            ('trace', '/fetch'),
            ('connect', '/fetch'),
            ('propfind', '/fetch'),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.request(method.upper(), path)
                self.assert_v12_headers(response)
                if path == '/fetch' or (path == '/health' and method == 'post'):
                    self.assertEqual(response.status_code, 405)
                    if path == '/fetch':
                        self.assertEqual(response.headers['allow'], 'POST')
                else:
                    self.assertEqual(response.status_code, 404)
                self.assertNotIn('payment-required', response.headers)

    def test_invalid_fetch_bodies_keep_existing_error_shapes(self):
        cases = (
            ({'content': b'', 'headers': {'Content-Type': 'application/json'}}, 'invalid_json'),
            ({'content': b'{', 'headers': {'Content-Type': 'application/json'}}, 'invalid_json'),
            ({'json': ['not', 'an', 'object']}, 'invalid_request'),
            ({'json': {}}, 'invalid_request'),
            ({'json': {'url': '   '}}, 'invalid_request'),
        )
        for request_kwargs, error_code in cases:
            with self.subTest(error_code=error_code, request=request_kwargs):
                response = self.client.post('/fetch', **request_kwargs)
                self.assertIn(response.status_code, (400, 422))
                self.assertEqual(response.json()['error_code'], error_code)
                self.assert_v12_headers(response)

        oversized = self.client.post('/fetch', content=b'x' * 20_001,
                                     headers={'Content-Type': 'application/json'})
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(oversized.json()['error_code'], 'invalid_body_size')
        self.assert_v12_headers(oversized)

    def test_free_fetch_preserves_success_augmentation(self):
        with patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS entered')) as dns:
            response = self.client.post('/fetch', json={
                'url': 'https://example.com/article',
                'force_browser': True,
                'max_chars': 1234,
            })
        self.assert_emergency_unavailable(response, 'browser_unavailable')
        self.assertNotIn('retrieved', response.text)
        dns.assert_not_called()

    def test_blocked_target_keeps_existing_error_mapping(self):
        with patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS entered')) as dns:
            response = self.client.post('/fetch', json={'url': 'http://127.0.0.1'})
        self.assert_emergency_unavailable(response)
        self.assertNotIn('127.0.0.1', response.text)
        dns.assert_not_called()

    def test_rate_limit_keeps_existing_error_and_retry_header(self):
        response = self.client.post('/fetch', json={'url': 'https://example.com'})
        self.assert_emergency_unavailable(response)
        self.assertNotEqual(response.headers['retry-after'], '60')

    def test_capacity_limit_keeps_existing_error_and_retry_header(self):
        response = self.client.post('/fetch', json={'url': 'https://example.com'})
        self.assert_emergency_unavailable(response)
        self.assertNotEqual(response.json()['error_code'], 'busy')

    def test_timeout_keeps_existing_error_mapping(self):
        with patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS entered')) as dns:
            response = self.client.post('/fetch', json={'url': 'https://example.com'})
        self.assert_emergency_unavailable(response)
        dns.assert_not_called()

    def test_fetch_failure_keeps_existing_error_mapping(self):
        response = self.client.post('/fetch', json={'url': 'https://example.com'})
        self.assert_emergency_unavailable(response)
        self.assertNotIn('upstream', response.text)

    def test_integrated_unpaid_response_has_matching_request_id(self):
        paid_settings = X402Settings(
            True,
            '0x1111111111111111111111111111111111111111',
            '$0.005',
            'eip155:84532',
        )

        with TestClient(server.create_app(paid_settings)) as client:
            response = client.post('/fetch', json={'url': 'https://example.com'})
            health = client.get('/health')

        self.assert_emergency_unavailable(response)
        self.assertEqual(health.status_code, 200)
        self.assert_v12_headers(health)


class UvicornProxyConfigurationTests(unittest.TestCase):
    def decoded_resource_url(self, environ):
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
        with patch.dict(os.environ, test_environ, clear=True):
            config = server.create_uvicorn_config(server.create_app(settings))
            config.load()
            with TestClient(
                config.loaded_app,
                base_url='http://test-host.example',
            ) as client:
                meta = client.get(
                    '/meta',
                    headers={
                        'Host': 'test-host.example',
                        'X-Forwarded-For': '203.0.113.10',
                        'X-Forwarded-Proto': 'https',
                    },
                )

                response = client.post('/fetch', json={'url': 'https://example.com'})

        self.assertEqual(meta.status_code, 200)
        self.assertFalse(meta.json()['retrieval_available'])
        self.assertFalse(meta.json()['fetch']['payment_challenge_enabled'])
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('payment-required', response.headers)
        return meta.json()['fetch']['url']

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
