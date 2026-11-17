import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solders.keypair import Keypair
from x402.http.utils import encode_payment_response_header
from x402.mechanisms.svm.exact import ExactSvmClientScheme
from x402.schemas import (
    NoMatchingRequirementsError,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
)

from scripts import paid_fetch_solana_mainnet_test as script


SOLANA_MAINNET = 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp'
SOLANA_USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
FEE_PAYER = 'BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP'
SELLER = '88hE4EB4yZwb4F2arPXcG7efwQyU5FDN26DuVpVKVk2W'


class FakeResponse:
    def __init__(self, status_code, body, settlement=None):
        self.status_code = status_code
        self._body = body
        self.headers = {}
        if settlement is not None:
            self.headers['payment-response'] = encode_payment_response_header(settlement)

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


def payment_required(**overrides):
    requirement = {
        'scheme': 'exact',
        'network': SOLANA_MAINNET,
        'asset': SOLANA_USDC,
        'amount': '10000',
        'payTo': SELLER,
        'maxTimeoutSeconds': 60,
        'extra': {'feePayer': FEE_PAYER},
    }
    requirement.update(overrides)
    return PaymentRequired(
        resource=ResourceInfo(url='https://example.test/fetch'),
        accepts=[PaymentRequirements(**requirement)],
    )


class SolanaClientConfigurationTests(unittest.TestCase):
    def test_client_registers_only_solana_mainnet_exact(self):
        client = script.build_x402_client(Keypair())
        self.assertEqual(
            client.get_registered_schemes(),
            {1: [], 2: [{'network': SOLANA_MAINNET, 'scheme': 'exact'}]},
        )

    def test_client_accepts_capped_offer_and_rejects_others(self):
        client = script.build_x402_client(Keypair())
        # Avoid live Solana RPC: stub only the transaction-building step.
        with patch.object(
            ExactSvmClientScheme,
            'create_payment_payload',
            return_value={'transaction': 'stub'},
        ):
            payload = client.create_payment_payload(payment_required())
            self.assertEqual(payload.accepted.network, SOLANA_MAINNET)
            self.assertEqual(payload.accepted.amount, '10000')

            rejected_offers = (
                {'amount': '10001'},
                {'network': 'solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1'},
                {'scheme': 'upto'},
            )
            for overrides in rejected_offers:
                with self.subTest(overrides=overrides), self.assertRaises(
                    NoMatchingRequirementsError
                ):
                    client.create_payment_payload(payment_required(**overrides))


class SolanaPaidFetchSafetyTests(unittest.TestCase):
    def test_prompted_key_stays_memory_only_and_request_is_fixed(self):
        buyer = Keypair()
        buyer_secret = str(buyer)  # base58 64-byte secret
        env_buyer = Keypair()
        env_secret = str(env_buyer)
        settlement = SettleResponse(
            success=True,
            payer=str(buyer.pubkey()),
            network=SOLANA_MAINNET,
            transaction='5solanatestsignature',
        )
        session = FakeSession(FakeResponse(200, {'success': True}, settlement))
        stdout, stderr = io.StringIO(), io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            original_directory = os.getcwd()
            try:
                os.chdir(temp_dir)
                with (
                    patch.dict(os.environ, {
                        'BUYER_SOLANA_PRIVATE_KEY': env_secret,
                        'SOLANA_PRIVATE_KEY': env_secret,
                    }),
                    patch.object(script.getpass, 'getpass', return_value=buyer_secret) as prompt,
                    patch.object(script, 'build_x402_client', return_value=object()),
                    patch.object(script, 'x402_requests', return_value=session),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = script.main([])
            finally:
                os.chdir(original_directory)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

        self.assertEqual(exit_code, 0)
        prompt.assert_called_once_with('Buyer Solana secret key (base58): ')
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(buyer_secret, output)
        self.assertNotIn(env_secret, output)
        self.assertNotIn(str(env_buyer.pubkey()), output)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(stdout.getvalue().splitlines(), [
            f'Buyer public address: {buyer.pubkey()}',
            'HTTP status: 200',
            'SmartFetch success: True',
            'Settlement success: True',
            f'Network: {SOLANA_MAINNET}',
            f'Payer public address: {buyer.pubkey()}',
            'Transaction signature: 5solanatestsignature',
        ])
        self.assertEqual(session.calls, [(
            'https://smartfetch-production-ea53.up.railway.app/fetch',
            {'json': {'url': 'https://example.com/'}, 'timeout': 120, 'allow_redirects': False},
        )])
        self.assertTrue(session.closed)

    def test_cli_arguments_are_rejected_before_secret_prompt(self):
        with (
            patch.object(script.getpass, 'getpass') as prompt,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = script.main(['--secret-key', 'not-accepted'])
        self.assertEqual(exit_code, 2)
        prompt.assert_not_called()

    def test_invalid_secret_key_is_not_echoed(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(script.getpass, 'getpass', return_value='not-a-valid-key'),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = script.main([])
        self.assertEqual(exit_code, 2)
        self.assertNotIn('not-a-valid-key', stdout.getvalue() + stderr.getvalue())

    def test_non_200_or_unsuccessful_settlement_exits_nonzero(self):
        buyer = Keypair()
        responses = (
            FakeResponse(503, {'success': False}),
            FakeResponse(200, {'success': True}, SettleResponse(
                success=False,
                errorReason='settlement_failed',
                payer=str(buyer.pubkey()),
                network=SOLANA_MAINNET,
                transaction='failed',
            )),
        )
        for response in responses:
            with self.subTest(status=response.status_code):
                session = FakeSession(response)
                with (
                    patch.object(script.getpass, 'getpass', return_value=str(buyer)),
                    patch.object(script, 'build_x402_client', return_value=object()),
                    patch.object(script, 'x402_requests', return_value=session),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertNotEqual(script.main([]), 0)
                self.assertTrue(session.closed)


if __name__ == '__main__':
    unittest.main()
