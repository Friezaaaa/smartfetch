"""Run one capped Solana mainnet x402 payment against SmartFetch.

The buyer secret key is requested interactively (hidden input) and is used only
to create an in-memory signer. This script accepts no command-line arguments,
reads no key from the environment, and writes no files.

Requirements for the buyer wallet:
- At least 0.01 USDC (SPL mint EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v)
  on Solana mainnet. No SOL is needed for fees: the facilitator is fee payer.
"""

from __future__ import annotations

import getpass
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Sequence

from solders.keypair import Keypair
from x402 import x402ClientSync
from x402.http.clients import x402_requests
from x402.http.constants import PAYMENT_RESPONSE_HEADER, X_PAYMENT_RESPONSE_HEADER
from x402.http.utils import decode_payment_response_header
from x402.mechanisms.svm.exact import ExactSvmClientScheme
from x402.mechanisms.svm.signers import KeypairSigner
from x402.schemas import SettleResponse


EXPECTED_X402_VERSION = '2.20.0'
SOLANA_MAINNET = 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp'
MAX_PAYMENT = '$0.01'
SMARTFETCH_URL = 'https://smartfetch-production-ea53.up.railway.app/fetch'
FETCH_PAYLOAD = {'url': 'https://example.com/'}
REQUEST_TIMEOUT_SECONDS = 120


def build_x402_client(keypair: Keypair) -> x402ClientSync:
    """Build an exact-payment client restricted to Solana mainnet."""
    signer = KeypairSigner(keypair)
    client = x402ClientSync()
    client.register(SOLANA_MAINNET, ExactSvmClientScheme(signer))
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

    print(f'HTTP status: {status_code}')
    print(f"SmartFetch success: {body.get('success') is True}")
    print(f'Settlement success: {settlement_success}')
    print(f'Network: {settlement.network if settlement else SOLANA_MAINNET}')
    print(
        'Payer public address: '
        f'{settlement.payer if settlement and settlement.payer else buyer_address}'
    )
    print(
        'Transaction signature: '
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

    secret_key = None
    keypair = None
    client = None
    session = None
    try:
        try:
            secret_key = getpass.getpass('Buyer Solana secret key (base58): ')
            keypair = Keypair.from_base58_string(secret_key.strip())
        except (EOFError, KeyboardInterrupt):
            print('\nSecret-key prompt cancelled.', file=sys.stderr)
            return 130
        except Exception:
            print('Invalid Buyer Solana secret key.', file=sys.stderr)
            return 2
        finally:
            secret_key = None

        buyer_address = str(keypair.pubkey())
        print(f'Buyer public address: {buyer_address}')

        client = build_x402_client(keypair)
        session = x402_requests(client)
        response = session.post(
            SMARTFETCH_URL,
            json=FETCH_PAYLOAD,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        return 0 if _print_result(response, buyer_address) else 1
    except Exception:
        print('Paid fetch failed.', file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.close()
        keypair = None
        client = None


if __name__ == '__main__':
    raise SystemExit(main())
