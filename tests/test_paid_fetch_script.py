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

from scripts import paid_fetch_test


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


class PaidFetchClientConfigurationTests(unittest.TestCase):
    def payment_required(self, account, **overrides):
        requirement = {
            'scheme': 'exact',
            'network': paid_fetch_test.BASE_SEPOLIA,
            'asset': '0x036CbD53842c5426634e7929541eC2318f3dCF7e',
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

    def test_client_enforces_base_sepolia_exact_and_one_cent_sdk_cap(self):
        account = Account.create()
        client = paid_fetch_test.build_x402_client(account)

        self.assertEqual(
            client.get_registered_schemes(),
            {
                1: [],
                2: [
                    {
                        'network': paid_fetch_test.BASE_SEPOLIA,
                        'scheme': 'exact',
                    }
                ],
            },
        )
        self.assertEqual(paid_fetch_test.MAX_PAYMENT, '$0.01')

        payload = client.create_payment_payload(self.payment_required(account))
        self.assertEqual(payload.accepted.amount, '10000')
        self.assertEqual(payload.accepted.network, paid_fetch_test.BASE_SEPOLIA)
        self.assertEqual(payload.accepted.scheme, 'exact')

        rejected_offers = (
            {'amount': '10001'},
            {'network': 'eip155:8453'},
            {'scheme': 'upto'},
        )
        for overrides in rejected_offers:
            with self.subTest(overrides=overrides), self.assertRaises(
                NoMatchingRequirementsError
            ):
                client.create_payment_payload(
                    self.payment_required(account, **overrides)
                )


class PaidFetchSafetyTests(unittest.TestCase):
    def test_private_key_is_prompted_memory_only_and_never_output(self):
        account = Account.create()
        private_key = account.key.hex()
        buyer_address = account.address
        settlement = SettleResponse(
            success=True,
            payer=buyer_address,
            network=paid_fetch_test.BASE_SEPOLIA,
            transaction='0xtesttransaction',
        )
        session = FakeSession(FakeResponse(200, {
            'success': True,
            'status_code': 200,
            'render_method': 'http',
            'word_count': 42,
            'final_url': 'https://example.com/',
        }, settlement))
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            original_directory = os.getcwd()
            try:
                os.chdir(temp_dir)
                with (
                    patch.object(
                        paid_fetch_test.getpass,
                        'getpass',
                        return_value=private_key,
                    ),
                    patch.object(
                        paid_fetch_test,
                        'build_x402_client',
                        return_value=object(),
                    ),
                    patch.object(
                        paid_fetch_test,
                        'x402_requests',
                        return_value=session,
                    ),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = paid_fetch_test.main([])
            finally:
                os.chdir(original_directory)

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertNotIn(private_key, output)
        self.assertIn(buyer_address, output)
        self.assertIn('HTTP status: 200', output)
        self.assertIn('SmartFetch success: True', output)
        self.assertIn('Settlement success: True', output)
        self.assertIn(paid_fetch_test.BASE_SEPOLIA, output)
        self.assertIn('0xtesttransaction', output)
        self.assertEqual(session.calls, [(
            paid_fetch_test.SMARTFETCH_URL,
            {
                'json': {'url': 'https://example.com/'},
                'timeout': paid_fetch_test.REQUEST_TIMEOUT_SECONDS,
                'allow_redirects': False,
            },
        )])
        self.assertTrue(session.closed)

    def test_command_line_arguments_are_rejected_before_secret_prompt(self):
        with (
            patch.object(paid_fetch_test.getpass, 'getpass') as prompt,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = paid_fetch_test.main(['--private-key', 'not-accepted'])

        self.assertEqual(exit_code, 2)
        prompt.assert_not_called()

    def test_non_200_or_unsuccessful_settlement_exits_nonzero(self):
        buyer_address = Account.create().address
        responses = (
            FakeResponse(503, {'success': False}),
            FakeResponse(200, {'success': True}, SettleResponse(
                success=False,
                errorReason='settlement_failed',
                payer=buyer_address,
                network=paid_fetch_test.BASE_SEPOLIA,
                transaction='0xfailed',
            )),
        )

        for response in responses:
            with self.subTest(status=response.status_code):
                session = FakeSession(response)
                with (
                    patch.object(
                        paid_fetch_test.getpass,
                        'getpass',
                        return_value=Account.create().key.hex(),
                    ),
                    patch.object(
                        paid_fetch_test,
                        'build_x402_client',
                        return_value=object(),
                    ),
                    patch.object(
                        paid_fetch_test,
                        'x402_requests',
                        return_value=session,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertNotEqual(paid_fetch_test.main([]), 0)


if __name__ == '__main__':
    unittest.main()
