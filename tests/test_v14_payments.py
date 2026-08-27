import contextlib
import io
import traceback
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import payments, server


VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
BASE_SEPOLIA = 'eip155:84532'
BASE_MAINNET = 'eip155:8453'
CDP_KEY_ID = 'organizations/test/apiKeys/test-key'
CDP_KEY_SECRET = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode('ascii')
MALFORMED_CDP_SECRET = 'malformed-cdp-secret-never-print'


def enabled_env(network=BASE_SEPOLIA, **overrides):
    values = {
        'X402_ENABLED': 'true',
        'X402_PAY_TO': VALID_ADDRESS,
        'X402_NETWORK': network,
    }
    values.update(overrides)
    return values


class TrackingEnvironment(dict):
    def __init__(self, values):
        super().__init__(values)
        self.accessed = []

    def get(self, key, default=None):
        self.accessed.append(key)
        return super().get(key, default)


class RecordingHttpClient:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self.payload = payload
        self.text = text
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        return False

    def get(self, url, headers):
        self.requests.append((url, headers))
        return self

    def json(self):
        return self.payload


class V14PaymentSettingsTests(unittest.TestCase):
    def test_mainnet_requires_both_cdp_credentials(self):
        cases = (
            {},
            {'CDP_API_KEY_ID': CDP_KEY_ID},
            {'CDP_API_KEY_SECRET': CDP_KEY_SECRET},
            {'CDP_API_KEY_ID': '   ', 'CDP_API_KEY_SECRET': CDP_KEY_SECRET},
            {'CDP_API_KEY_ID': CDP_KEY_ID, 'CDP_API_KEY_SECRET': '   '},
        )
        for credentials in cases:
            with self.subTest(credentials=tuple(credentials)), self.assertRaisesRegex(
                ValueError, 'CDP_API_KEY_ID and CDP_API_KEY_SECRET'
            ):
                payments.load_x402_settings(enabled_env(
                    BASE_MAINNET,
                    **credentials,
                ))

    def test_exactly_base_sepolia_and_base_mainnet_are_supported(self):
        sepolia = payments.load_x402_settings(enabled_env())
        self.assertEqual(sepolia.network, BASE_SEPOLIA)

        mainnet = payments.load_x402_settings(enabled_env(
            BASE_MAINNET,
            CDP_API_KEY_ID=CDP_KEY_ID,
            CDP_API_KEY_SECRET=CDP_KEY_SECRET,
        ))
        self.assertEqual(mainnet.network, BASE_MAINNET)

        for network in ('eip155:1', 'eip155:84531', 'base', 'base-sepolia'):
            with self.subTest(network=network), self.assertRaisesRegex(
                ValueError, 'X402_NETWORK'
            ):
                payments.load_x402_settings(enabled_env(network))

    def test_cdp_credentials_are_not_exposed_or_wallet_secret_read(self):
        values = TrackingEnvironment(enabled_env(
            BASE_MAINNET,
            CDP_API_KEY_ID=CDP_KEY_ID,
            CDP_API_KEY_SECRET=CDP_KEY_SECRET,
            CDP_WALLET_SECRET='must-not-be-read',
        ))

        settings = payments.load_x402_settings(values)

        self.assertNotIn(CDP_KEY_ID, repr(settings))
        self.assertNotIn(CDP_KEY_SECRET, repr(settings))
        self.assertNotIn('CDP_WALLET_SECRET', values.accessed)


class V14FacilitatorSelectionTests(unittest.TestCase):
    def test_sepolia_uses_x402_org_without_cdp_authentication(self):
        settings = payments.load_x402_settings(enabled_env())

        facilitator = payments.create_facilitator(settings)

        self.assertEqual(facilitator._url, 'https://x402.org/facilitator')
        self.assertIsNone(facilitator._auth_provider)

    def test_mainnet_uses_authenticated_official_cdp_facilitator(self):
        settings = payments.load_x402_settings(enabled_env(
            BASE_MAINNET,
            CDP_API_KEY_ID=CDP_KEY_ID,
            CDP_API_KEY_SECRET=CDP_KEY_SECRET,
        ))
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            from cdp.x402 import create_facilitator_config as official_config

            with patch(
                'cdp.x402.create_facilitator_config',
                wraps=official_config,
            ) as create_config:
                facilitator = payments.create_facilitator(settings)

        create_config.assert_called_once_with(CDP_KEY_ID, CDP_KEY_SECRET)
        self.assertEqual(
            facilitator._url,
            'https://api.cdp.coinbase.com/platform/v2/x402',
        )
        self.assertIsNotNone(facilitator._auth_provider)
        self.assertNotIn(CDP_KEY_ID, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(CDP_KEY_SECRET, stdout.getvalue() + stderr.getvalue())

    def test_mainnet_initialization_error_is_sanitized(self):
        settings = payments.load_x402_settings(enabled_env(
            BASE_MAINNET,
            CDP_API_KEY_ID=CDP_KEY_ID,
            CDP_API_KEY_SECRET=CDP_KEY_SECRET,
        ))
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            patch(
                'cdp.x402.create_facilitator_config',
                side_effect=RuntimeError(
                    f'invalid credential {CDP_KEY_ID} {CDP_KEY_SECRET}'
                ),
            ),
            self.assertRaisesRegex(RuntimeError, '^x402 initialization failed$') as caught,
        ):
            payments.install_x402(FastAPI(), settings)

        exposed = ''.join(traceback.format_exception(caught.exception))
        exposed += stdout.getvalue() + stderr.getvalue()
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(CDP_KEY_ID, exposed)
        self.assertNotIn(CDP_KEY_SECRET, exposed)

    def test_mainnet_requires_literal_v2_exact_capability(self):
        settings = payments.load_x402_settings(enabled_env(
            BASE_MAINNET,
            CDP_API_KEY_ID=CDP_KEY_ID,
            CDP_API_KEY_SECRET=CDP_KEY_SECRET,
        ))

        unsupported_kinds = (
            SupportedKind(
                x402Version=2,
                scheme='exact',
                network=BASE_SEPOLIA,
            ),
            SupportedKind(
                x402Version=2,
                scheme='exact',
                network='eip155:*',
            ),
            SupportedKind(
                x402Version=1,
                scheme='exact',
                network=BASE_MAINNET,
            ),
            SupportedKind(
                x402Version=2,
                scheme='upto',
                network=BASE_MAINNET,
            ),
        )
        for kind in unsupported_kinds:
            with (
                self.subTest(kind=kind),
                patch(
                    'x402.http.HTTPFacilitatorClient.get_supported',
                    return_value=SupportedResponse(kinds=[kind]),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    '^x402 initialization failed$',
                ),
            ):
                payments.install_x402(FastAPI(), settings)

    def test_mainnet_preflight_uses_real_cdp_auth_header_path(self):
        settings = payments.load_x402_settings(enabled_env(
            BASE_MAINNET,
            CDP_API_KEY_ID=CDP_KEY_ID,
            CDP_API_KEY_SECRET=CDP_KEY_SECRET,
        ))
        transport = RecordingHttpClient(payload={
            'kinds': [{
                'x402Version': 2,
                'scheme': 'exact',
                'network': BASE_MAINNET,
            }],
        })

        with patch(
            'x402.http.HTTPFacilitatorClient._get_sync_client',
            return_value=transport,
        ):
            self.assertTrue(payments.install_x402(FastAPI(), settings))

        self.assertEqual(len(transport.requests), 1)
        url, headers = transport.requests[0]
        self.assertEqual(
            url,
            'https://api.cdp.coinbase.com/platform/v2/x402/supported',
        )
        self.assertTrue(headers['Authorization'].startswith('Bearer '))
        self.assertNotIn(CDP_KEY_ID, repr(headers))
        self.assertNotIn(CDP_KEY_SECRET, repr(headers))

    def test_mainnet_auth_and_http_failures_are_closed_and_sanitized(self):
        cases = (
            (
                CDP_KEY_SECRET,
                RecordingHttpClient(status_code=401, text='unauthorized'),
            ),
            (MALFORMED_CDP_SECRET, RecordingHttpClient()),
        )
        for secret, transport in cases:
            settings = payments.load_x402_settings(enabled_env(
                BASE_MAINNET,
                CDP_API_KEY_ID=CDP_KEY_ID,
                CDP_API_KEY_SECRET=secret,
            ))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                self.subTest(secret_is_valid=secret == CDP_KEY_SECRET),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                patch(
                    'x402.http.HTTPFacilitatorClient._get_sync_client',
                    return_value=transport,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    '^x402 initialization failed$',
                ) as caught,
            ):
                payments.install_x402(FastAPI(), settings)

            exposed = ''.join(traceback.format_exception(caught.exception))
            exposed += stdout.getvalue() + stderr.getvalue()
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn(CDP_KEY_ID, exposed)
            self.assertNotIn(secret, exposed)
            self.assertTrue(all(
                request_url.startswith(
                    'https://api.cdp.coinbase.com/platform/v2/x402/'
                )
                for request_url, _headers in transport.requests
            ))


class V14RouteAndMetadataTests(unittest.TestCase):
    @patch('x402.http.HTTPFacilitatorClient.get_supported')
    def test_mainnet_protects_only_post_fetch_and_reports_mainnet(
        self, get_supported
    ):
        get_supported.return_value = SupportedResponse(kinds=[SupportedKind(
            x402Version=2,
            scheme='exact',
            network=BASE_MAINNET,
        )])
        settings = payments.load_x402_settings(enabled_env(
            BASE_MAINNET,
            CDP_API_KEY_ID=CDP_KEY_ID,
            CDP_API_KEY_SECRET=CDP_KEY_SECRET,
        ))

        with TestClient(server.create_app(settings)) as client:
            health = client.get('/health')
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()['version'], '1.9.0')
            self.assertEqual(client.get('/').status_code, 200)
            meta = client.get('/meta')
            self.assertEqual(meta.status_code, 200)
            self.assertEqual(meta.json()['version'], '1.9.0')
            self.assertEqual(meta.json()['payment'], 'x402-enabled-mainnet')
            self.assertEqual(client.get('/fetch').status_code, 404)
            self.assertEqual(client.post('/fetch').status_code, 402)

    @patch('x402.http.HTTPFacilitatorClient.get_supported')
    def test_sepolia_meta_remains_testnet(self, get_supported):
        get_supported.return_value = SupportedResponse(kinds=[SupportedKind(
            x402Version=2,
            scheme='exact',
            network=BASE_SEPOLIA,
        )])
        settings = payments.load_x402_settings(enabled_env())

        with TestClient(server.create_app(settings)) as client:
            self.assertEqual(
                client.get('/meta').json()['payment'],
                'x402-enabled-testnet',
            )


if __name__ == '__main__':
    unittest.main()
