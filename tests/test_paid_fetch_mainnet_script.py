import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_account import Account
from x402.http.utils import encode_payment_response_header
from x402.schemas import (
    NoMatchingRequirementsError,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
)

try:
    from scripts import paid_fetch_mainnet_test
except ImportError:
    paid_fetch_mainnet_test = None


class FakeResponse:
    def __init__(self, status_code, body, settlement=None):
        self.status_code = status_code
        self._body = body
        self.headers = {}
        if settlement is not None:
            self.headers['payment-response'] = encode_payment_response_header(
                settlement
            )

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class MainnetClientConfigurationTests(unittest.TestCase):
    def payment_required(self, account, **overrides):
        requirement = {
            'scheme': 'exact',
            'network': 'eip155:8453',
            'asset': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
            'amount': '10000',
            'payTo': account.address,
            'maxTimeoutSeconds': 60,
            'extra': {'name': 'USDC', 'version': '2'},
        }
        requirement.update(overrides)
        return PaymentRequired(
            resource=ResourceInfo(url='https://example.test/fetch'),
            accepts=[PaymentRequirements(**requirement)],
        )

    def test_client_accepts_only_capped_base_mainnet_exact_payments(self):
        self.assertIsNotNone(
            paid_fetch_mainnet_test,
            'scripts/paid_fetch_mainnet_test.py must exist',
        )
        account = Account.create()
        client = paid_fetch_mainnet_test.build_x402_client(account)

        self.assertEqual(
            client.get_registered_schemes(),
            {
                1: [],
                2: [{'network': 'eip155:8453', 'scheme': 'exact'}],
            },
        )

        payload = client.create_payment_payload(self.payment_required(account))
        self.assertEqual(payload.accepted.amount, '10000')
        self.assertEqual(payload.accepted.network, 'eip155:8453')
        self.assertEqual(payload.accepted.scheme, 'exact')

        rejected_offers = (
            {'amount': '10001'},
            {'network': 'eip155:84532'},
            {'scheme': 'upto'},
        )
        for overrides in rejected_offers:
            with self.subTest(overrides=overrides), self.assertRaises(
                NoMatchingRequirementsError
            ):
                client.create_payment_payload(
                    self.payment_required(account, **overrides)
                )


@unittest.skipIf(
    paid_fetch_mainnet_test is None,
    'mainnet smoke-test script is not implemented yet',
)
class MainnetPaidFetchSafetyTests(unittest.TestCase):
    def test_prompted_key_stays_memory_only_and_request_is_fixed(self):
        prompted_account = Account.create()
        prompted_key = prompted_account.key.hex()
        environment_account = Account.create()
        environment_key = environment_account.key.hex()
        settlement = SettleResponse(
            success=True,
            payer=prompted_account.address,
            network='eip155:8453',
            transaction='0xmainnettesttransaction',
        )
        session = FakeSession(FakeResponse(200, {'success': True}, settlement))
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            original_directory = os.getcwd()
            try:
                os.chdir(temp_dir)
                with (
                    patch.dict(os.environ, {
                        'BUYER_EVM_PRIVATE_KEY': environment_key,
                        'PRIVATE_KEY': environment_key,
                    }),
                    patch.object(
                        paid_fetch_mainnet_test.getpass,
                        'getpass',
                        return_value=prompted_key,
                    ) as prompt,
                    patch.object(
                        paid_fetch_mainnet_test,
                        'build_x402_client',
                        return_value=object(),
                    ),
                    patch.object(
                        paid_fetch_mainnet_test,
                        'x402_requests',
                        return_value=session,
                    ),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = paid_fetch_mainnet_test.main([])
            finally:
                os.chdir(original_directory)

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

        self.assertEqual(exit_code, 0)
        prompt.assert_called_once_with('Buyer EVM private key: ')
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(prompted_key, output)
        self.assertNotIn(environment_key, output)
        self.assertNotIn(environment_account.address, output)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(stdout.getvalue().splitlines(), [
            f'Buyer public address: {prompted_account.address}',
            'HTTP status: 200',
            'SmartFetch success: True',
            'Settlement success: True',
            'Network: eip155:8453',
            f'Payer public address: {prompted_account.address}',
            'Transaction hash: 0xmainnettesttransaction',
        ])
        self.assertEqual(session.calls, [(
            'https://smartfetch-production-ea53.up.railway.app/fetch',
            {
                'json': {'url': 'https://example.com/'},
                'timeout': 120,
                'allow_redirects': False,
            },
        )])
        self.assertTrue(session.closed)

    def test_cli_arguments_are_rejected_before_secret_prompt(self):
        with (
            patch.object(paid_fetch_mainnet_test.getpass, 'getpass') as prompt,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = paid_fetch_mainnet_test.main([
                '--private-key',
                'not-accepted',
            ])

        self.assertEqual(exit_code, 2)
        prompt.assert_not_called()

    def test_non_200_or_unsuccessful_settlement_exits_nonzero(self):
        buyer = Account.create()
        responses = (
            FakeResponse(503, {'success': False}),
            FakeResponse(200, {'success': True}, SettleResponse(
                success=False,
                errorReason='settlement_failed',
                payer=buyer.address,
                network='eip155:8453',
                transaction='0xfailed',
            )),
        )

        for response in responses:
            with self.subTest(status=response.status_code):
                session = FakeSession(response)
                with (
                    patch.object(
                        paid_fetch_mainnet_test.getpass,
                        'getpass',
                        return_value=buyer.key.hex(),
                    ),
                    patch.object(
                        paid_fetch_mainnet_test,
                        'build_x402_client',
                        return_value=object(),
                    ),
                    patch.object(
                        paid_fetch_mainnet_test,
                        'x402_requests',
                        return_value=session,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertNotEqual(paid_fetch_mainnet_test.main([]), 0)
                self.assertTrue(session.closed)


if __name__ == '__main__':
    unittest.main()
