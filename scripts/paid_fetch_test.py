"""Run one capped Base Sepolia x402 payment against SmartFetch.

The buyer private key is requested interactively and is used only to create an
in-memory signer. This script accepts no command-line arguments and writes no
files.
"""

from __future__ import annotations

import getpass
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Sequence

from eth_account import Account
from eth_account.signers.local import LocalAccount
from x402 import x402ClientSync
from x402.http.clients import x402_requests
from x402.http.constants import PAYMENT_RESPONSE_HEADER, X_PAYMENT_RESPONSE_HEADER
from x402.http.utils import decode_payment_response_header
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import SettleResponse


EXPECTED_X402_VERSION = '2.20.0'
BASE_SEPOLIA = 'eip155:84532'
MAX_PAYMENT = '$0.01'
SMARTFETCH_URL = 'https://smartfetch-production-ea53.up.railway.app/fetch'
FETCH_PAYLOAD = {'url': 'https://example.com/'}
REQUEST_TIMEOUT_SECONDS = 120


def build_x402_client(account: LocalAccount) -> x402ClientSync:
    """Build an exact-payment client restricted to Base Sepolia."""
    signer = EthAccountSigner(account)
    client = x402ClientSync()
    client.register(BASE_SEPOLIA, ExactEvmClientScheme(signer))
    client.set_spend_controls({'max_amount_per_payment': MAX_PAYMENT})
    return client


def _installed_x402_version() -> str | None:
    try:
        return version('x402')
    except PackageNotFoundError:
        return None


def _decode_settlement(headers: object) -> SettleResponse | None:
    getter = getattr(headers, 'get', None)
    if not callable(getter):
        return None

    encoded = None
    for name in (PAYMENT_RESPONSE_HEADER, X_PAYMENT_RESPONSE_HEADER):
        encoded = getter(name) or getter(name.lower())
        if encoded:
            break
    if not isinstance(encoded, str):
        return None

    try:
        return decode_payment_response_header(encoded)
    except (TypeError, ValueError):
        return None


def _response_body(response: object) -> dict[str, object]:
    try:
        body = response.json()
    except (AttributeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _print_result(response: object, buyer_address: str) -> bool:
    status_code = getattr(response, 'status_code', 0)
    body = _response_body(response)
    settlement = _decode_settlement(getattr(response, 'headers', {}))
    settlement_success = bool(settlement and settlement.success)

    summary_keys = (
        'status_code',
        'render_method',
        'word_count',
        'final_url',
        'elapsed_ms',
    )
    summary = {key: body[key] for key in summary_keys if key in body}

    print(f'HTTP status: {status_code}')
    print(f"SmartFetch success: {body.get('success') is True}")
    print(f'Result summary: {json.dumps(summary, sort_keys=True)}')
    print(f'Settlement success: {settlement_success}')
    print(
        'Payer public address: '
        f'{settlement.payer if settlement and settlement.payer else buyer_address}'
    )
    print(f'Network: {settlement.network if settlement else BASE_SEPOLIA}')
    print(
        'Transaction hash: '
        f'{settlement.transaction if settlement and settlement.transaction else "unavailable"}'
    )

    return status_code == 200 and settlement_success


def main(argv: Sequence[str] | None = None) -> int:
    """Prompt for a buyer key, make the paid request, and return an exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print('This script accepts no command-line arguments.', file=sys.stderr)
        return 2

    installed_version = _installed_x402_version()
    if installed_version != EXPECTED_X402_VERSION:
        print(
            f'x402 {EXPECTED_X402_VERSION} is required; installed: '
            f'{installed_version or "not found"}.',
            file=sys.stderr,
        )
        return 2

    private_key = None
    account = None
    client = None
    session = None
    try:
        try:
            private_key = getpass.getpass('Buyer EVM private key: ')
            account = Account.from_key(private_key)
        except (EOFError, KeyboardInterrupt):
            print('\nPrivate-key prompt cancelled.', file=sys.stderr)
            return 130
        except Exception:
            print('Invalid Buyer EVM private key.', file=sys.stderr)
            return 2
        finally:
            private_key = None

        buyer_address = account.address
        print(f'Buyer public address: {buyer_address}')

        client = build_x402_client(account)
        session = x402_requests(client)
        response = session.post(
            SMARTFETCH_URL,
            json=FETCH_PAYLOAD,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        return 0 if _print_result(response, buyer_address) else 1
    except Exception as error:
        print(f'Paid fetch failed ({type(error).__name__}).', file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.close()
        account = None
        client = None


if __name__ == '__main__':
    raise SystemExit(main())
