import importlib
import importlib.util
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse


VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
BASE_SEPOLIA = 'eip155:84532'


def payments_module():
    spec = importlib.util.find_spec('smartfetch.payments')
    if spec is None:
        raise AssertionError('smartfetch.payments must exist')
    return importlib.import_module('smartfetch.payments')


def payment_test_app():
    app = FastAPI()

    @app.get('/')
    def root():
        return {'service': 'smartfetch'}

    @app.get('/health')
    def health():
        return {'ok': True}

    @app.get('/meta')
    def meta():
        return {'service': 'smartfetch'}

    @app.post('/fetch')
    def fetch():
        return {'ok': True}

    return app


class PaymentSettingsTests(unittest.TestCase):
    def test_unset_is_free_with_testnet_defaults(self):
        payments = payments_module()
        settings = payments.load_x402_settings({})
        self.assertEqual(
            settings,
            payments.X402Settings(False, None, '$0.005', BASE_SEPOLIA),
        )

    def test_disabled_mode_ignores_invalid_optional_payment_values(self):
        payments = payments_module()
        settings = payments.load_x402_settings({
            'X402_ENABLED': 'false',
            'X402_PAY_TO': 'not-an-address',
            'X402_PRICE': 'free',
            'X402_NETWORK': 'eip155:8453',
        })
        self.assertFalse(settings.enabled)

    def test_recognized_boolean_values_are_case_insensitive(self):
        payments = payments_module()
        for value in ('1', 'true', 'TRUE', 'yes', 'On'):
            with self.subTest(value=value):
                settings = payments.load_x402_settings({
                    'X402_ENABLED': value,
                    'X402_PAY_TO': VALID_ADDRESS,
                })
                self.assertTrue(settings.enabled)
        for value in ('', '0', 'false', 'FALSE', 'no', 'Off'):
            with self.subTest(value=value):
                settings = payments.load_x402_settings({'X402_ENABLED': value})
                self.assertFalse(settings.enabled)

    def test_enabled_mode_uses_approved_defaults(self):
        payments = payments_module()
        settings = payments.load_x402_settings({
            'X402_ENABLED': 'true',
            'X402_PAY_TO': VALID_ADDRESS,
        })
        self.assertEqual(settings.price, '$0.005')
        self.assertEqual(settings.network, BASE_SEPOLIA)
        self.assertEqual(settings.pay_to, VALID_ADDRESS)

    def test_invalid_enable_value_fails_closed(self):
        payments = payments_module()
        with self.assertRaisesRegex(ValueError, 'X402_ENABLED'):
            payments.load_x402_settings({'X402_ENABLED': 'sometimes'})

    def test_missing_or_invalid_receiving_address_fails_closed(self):
        payments = payments_module()
        invalid_addresses = (
            None,
            '',
            '0x1234',
            '1111111111111111111111111111111111111111',
            '0xgggggggggggggggggggggggggggggggggggggggg',
            '0x0000000000000000000000000000000000000000',
        )
        for address in invalid_addresses:
            env = {'X402_ENABLED': 'true'}
            if address is not None:
                env['X402_PAY_TO'] = address
            with self.subTest(address=address), self.assertRaisesRegex(
                ValueError, 'X402_PAY_TO'
            ):
                payments.load_x402_settings(env)

    def test_invalid_or_nonpositive_price_fails_closed(self):
        payments = payments_module()
        for price in ('', '0.005', '$0', '$0.0000000', '$-1', '$abc', '$1.2.3', '$00.5'):
            with self.subTest(price=price), self.assertRaisesRegex(
                ValueError, 'X402_PRICE'
            ):
                payments.load_x402_settings({
                    'X402_ENABLED': 'true',
                    'X402_PAY_TO': VALID_ADDRESS,
                    'X402_PRICE': price,
                })

    def test_base_mainnet_and_every_other_network_fail_closed(self):
        payments = payments_module()
        for network in ('eip155:8453', 'base-sepolia', 'eip155:1', 'solana:devnet'):
            with self.subTest(network=network), self.assertRaisesRegex(
                ValueError, 'X402_NETWORK'
            ):
                payments.load_x402_settings({
                    'X402_ENABLED': 'true',
                    'X402_PAY_TO': VALID_ADDRESS,
                    'X402_NETWORK': network,
                })


class PaymentMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.payments = payments_module()

    def test_disabled_mode_leaves_fetch_free(self):
        app = payment_test_app()
        installed = self.payments.install_x402(
            app, self.payments.load_x402_settings({})
        )

        self.assertFalse(installed)
        with TestClient(app) as client:
            self.assertEqual(client.post('/fetch').status_code, 200)

    @patch('x402.http.HTTPFacilitatorClient.get_supported')
    def test_enabled_mode_protects_only_post_fetch(self, get_supported):
        get_supported.return_value = SupportedResponse(kinds=[SupportedKind(
            x402Version=2,
            scheme='exact',
            network=BASE_SEPOLIA,
        )])
        app = payment_test_app()
        settings = self.payments.load_x402_settings({
            'X402_ENABLED': 'true',
            'X402_PAY_TO': VALID_ADDRESS,
        })

        self.assertTrue(self.payments.install_x402(app, settings))
        with TestClient(app) as client:
            self.assertEqual(client.get('/').status_code, 200)
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertEqual(client.get('/meta').status_code, 200)
            self.assertEqual(client.post('/fetch').status_code, 402)

    @patch('x402.http.HTTPFacilitatorClient.get_supported')
    def test_facilitator_initialization_failure_aborts_startup(
        self, get_supported
    ):
        get_supported.side_effect = RuntimeError('facilitator unavailable')
        settings = self.payments.load_x402_settings({
            'X402_ENABLED': 'true',
            'X402_PAY_TO': VALID_ADDRESS,
        })

        with self.assertRaisesRegex(RuntimeError, 'x402 initialization failed'):
            self.payments.install_x402(payment_test_app(), settings)


if __name__ == '__main__':
    unittest.main()
