import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from x402.schemas import PaymentRequired, PaymentRequirements


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_EXAMPLE = REPO_ROOT / 'examples/python/paid_mcp_client.py'
TYPESCRIPT_EXAMPLE = REPO_ROOT / 'examples/typescript/paid-mcp-client.ts'
BASE_MAINNET_USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'


class FakeSigner:
    @property
    def address(self):
        return '0x2222222222222222222222222222222222222222'

    def sign_typed_data(self, *_args, **_kwargs):
        raise AssertionError('an over-limit challenge must be rejected first')


def load_python_example():
    spec = importlib.util.spec_from_file_location(
        'smartfetch_paid_mcp_example',
        PYTHON_EXAMPLE,
    )
    if spec is None or spec.loader is None:
        raise AssertionError('unable to load Python buyer example')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PythonBuyerExampleTests(unittest.TestCase):
    def test_import_has_no_credentials_or_network_side_effects(self):
        with patch.dict(os.environ, {}, clear=True):
            module = load_python_example()

        self.assertTrue(callable(module.main))

    def test_payment_client_rejects_any_challenge_above_half_cent(self):
        module = load_python_example()
        client = module.build_payment_client(FakeSigner())
        challenge = PaymentRequired(accepts=[PaymentRequirements(
            scheme='exact',
            network='eip155:8453',
            asset=BASE_MAINNET_USDC,
            amount='5001',
            payTo='0x1111111111111111111111111111111111111111',
            maxTimeoutSeconds=60,
            extra={'name': 'USD Coin', 'version': '2'},
        )])

        with self.assertRaisesRegex(
            Exception,
            'max_amount_per_payment',
        ):
            asyncio.run(client.create_payment_payload(challenge))


class TypeScriptBuyerExampleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is not installed')
    def test_example_passes_node_typescript_syntax_check(self):
        result = subprocess.run(
            ['node', '--check', str(TYPESCRIPT_EXAMPLE)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
