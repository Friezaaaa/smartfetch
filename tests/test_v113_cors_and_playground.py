"""Tests for v1.13 CORS preflight support and Phase 1 Playground single-file application."""

from pathlib import Path
import unittest
from starlette.testclient import TestClient

from smartfetch.server import create_app
from smartfetch.payments import X402Settings


FREE_SETTINGS = X402Settings(
    enabled=False,
    pay_to=None,
    price="$0.005",
    network="eip155:8453",
)


class TestCORSPreflightAndHeaders(unittest.TestCase):
    def setUp(self):
        self.app = create_app(FREE_SETTINGS)

    def test_options_preflight_on_fetch_and_mcp(self):
        """OPTIONS /fetch and OPTIONS /mcp return 200 OK with Access-Control-Allow-Origin: * and allow PAYMENT-SIGNATURE / Accept."""
        with TestClient(self.app) as client:
            # 1. OPTIONS /fetch preflight
            r_fetch = client.options(
                "/fetch",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type, PAYMENT-SIGNATURE",
                },
            )
            self.assertEqual(r_fetch.status_code, 200)
            self.assertEqual(r_fetch.headers.get("access-control-allow-origin"), "*")
            allow_headers = r_fetch.headers.get("access-control-allow-headers", "").lower()
            self.assertIn("payment-signature", allow_headers)
            self.assertIn("content-type", allow_headers)

            # 2. OPTIONS /mcp preflight
            r_mcp = client.options(
                "/mcp",
                headers={
                    "Origin": "https://glama.ai",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type, Accept",
                },
            )
            self.assertEqual(r_mcp.status_code, 200)
            self.assertEqual(r_mcp.headers.get("access-control-allow-origin"), "*")
            allow_headers_mcp = r_mcp.headers.get("access-control-allow-headers", "").lower()
            self.assertIn("accept", allow_headers_mcp)

    def test_post_fetch_exposes_payment_headers(self):
        """POST /fetch responses include Access-Control-Expose-Headers with PAYMENT-REQUIRED and PAYMENT-RESPONSE."""
        with TestClient(self.app) as client:
            r = client.post(
                "/fetch",
                json={"url": "https://example.com"},
                headers={"Origin": "http://localhost:3000"},
            )
            # Response must have CORS origin header
            self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
            expose = r.headers.get("access-control-expose-headers", "")
            self.assertIn("PAYMENT-REQUIRED", expose)
            self.assertIn("PAYMENT-RESPONSE", expose)
            self.assertIn("X-Request-Id", expose)

    def test_non_post_non_options_methods_return_405(self):
        """GET, PUT, DELETE, PATCH on /fetch and /mcp continue returning 405 Method Not Allowed."""
        with TestClient(self.app) as client:
            r_get_fetch = client.get("/fetch", headers={"Origin": "http://localhost:3000"})
            self.assertEqual(r_get_fetch.status_code, 405)
            self.assertEqual(r_get_fetch.headers.get("access-control-allow-origin"), "*")

            r_put_fetch = client.put("/fetch", json={}, headers={"Origin": "http://localhost:3000"})
            self.assertEqual(r_put_fetch.status_code, 405)

            r_del_mcp = client.delete("/mcp", headers={"Origin": "https://glama.ai"})
            self.assertEqual(r_del_mcp.status_code, 405)


class TestPlaygroundSandbox(unittest.TestCase):
    def test_playground_file_exists_and_strictly_sandboxed(self):
        """examples/playground/index.html exists and strictly uses sandbox='allow-scripts' without allow-same-origin."""
        repo_root = Path(__file__).resolve().parent.parent
        playground_path = repo_root / "examples" / "playground" / "index.html"

        self.assertTrue(playground_path.is_file(), f"Playground file not found at {playground_path}")
        content = playground_path.read_text(encoding="utf-8")

        # Iframe must have sandbox="allow-scripts"
        self.assertIn('sandbox="allow-scripts"', content)

        # Must strictly omit dangerous capabilities
        self.assertNotIn("allow-same-origin", content)
        self.assertNotIn("allow-top-navigation", content)

    def test_playground_ssrf_and_eip712_structure(self):
        """examples/playground/index.html includes client-side SSRF defenses and EIP-712 USDC TransferWithAuthorization."""
        repo_root = Path(__file__).resolve().parent.parent
        playground_path = repo_root / "examples" / "playground" / "index.html"
        content = playground_path.read_text(encoding="utf-8")

        # SSRF checks
        self.assertIn("function isSSRF", content)
        self.assertIn("169.254.", content)
        self.assertIn("localhost", content)
        self.assertIn("SSRF Blocked", content)

        # Web3 / EIP-712 USDC contract definitions
        self.assertIn("TransferWithAuthorization", content)
        self.assertIn("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", content)
        self.assertIn("0x2105", content)
        self.assertIn("8453", content)
        self.assertIn("x402Version: 2", content)

    def test_playground_solana_dual_chain_structure(self):
        """examples/playground/index.html includes Solana SVM Wallet Standard, mint, and payTo definitions."""
        repo_root = Path(__file__).resolve().parent.parent
        playground_path = repo_root / "examples" / "playground" / "index.html"
        content = playground_path.read_text(encoding="utf-8")

        # Solana network CAIP-2 & Asset
        self.assertIn("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", content)
        self.assertIn("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", content)
        self.assertIn("88hE4EB4yZwb4F2arPXcG7efwQyU5FDN26DuVpVKVk2W", content)
        self.assertIn("BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP", content)

        # Solana Wallet Standard & transaction building
        self.assertIn("wallet-standard:register-wallet", content)
        self.assertIn("VersionedTransaction", content)
        self.assertIn("solscan.io/tx/", content)
        self.assertIn("solanaRpcUrl", content)
        self.assertIn("performSolanaPreflightChecks", content)
        self.assertIn("decodePaymentRequiredError", content)
        self.assertIn("normalizeSolanaPubkey", content)
        self.assertIn("normalizeSignedTxBytes", content)
        self.assertIn("<summary", content)
        self.assertIn("Advanced options</summary>", content)
        self.assertIn("Include raw HTML code", content)
        self.assertIn("Needed for the Sandboxed HTML Preview tab", content)
        self.assertIn("Force fresh fetch (skip 60s cache)", content)
        self.assertIn("Re-downloads live instead of reusing a 60s saved copy", content)

    def test_playground_solana_transaction_layout_passes_x402_svm_verifier(self):
        """Verifies the exact 4-instruction VersionedTransaction layout built by Playground JS passes x402==2.20.0 ExactSvmScheme.verify()."""
        import base64
        import binascii
        import os
        import struct
        from unittest.mock import MagicMock

        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.keypair import Keypair
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.signature import Signature
        from solders.transaction import VersionedTransaction
        from x402.mechanisms.svm.exact.facilitator import ExactSvmScheme as ExactSvmFacilitatorScheme
        from x402.mechanisms.svm.utils import derive_ata
        from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo

        fee_payer_str = "BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP"
        seller_str = "88hE4EB4yZwb4F2arPXcG7efwQyU5FDN26DuVpVKVk2W"
        mint_str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        token_prog_str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        compute_budget_str = "ComputeBudget111111111111111111111111111111"
        memo_prog_str = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

        buyer_kp = Keypair()
        buyer_pubkey = buyer_kp.pubkey()
        fee_payer = Pubkey.from_string(fee_payer_str)
        mint = Pubkey.from_string(mint_str)
        token_prog = Pubkey.from_string(token_prog_str)

        buyer_ata = Pubkey.from_string(derive_ata(str(buyer_pubkey), mint_str, token_prog_str))
        seller_ata = Pubkey.from_string(derive_ata(seller_str, mint_str, token_prog_str))

        # Mirror Playground JS buildSolanaTransaction() byte-for-byte:
        # 1. SetComputeUnitLimit(20000) -> [2, u32le(20000)]
        cu_limit_ix = Instruction(
            Pubkey.from_string(compute_budget_str),
            b"\x02" + struct.pack("<I", 20000),
            [],
        )
        # 2. SetComputeUnitPrice(1) -> [3, u64le(1)]
        cu_price_ix = Instruction(
            Pubkey.from_string(compute_budget_str),
            b"\x03" + struct.pack("<Q", 1),
            [],
        )
        # 3. SPL TransferChecked(5000, decimals=6) -> [12, u64le(5000), 6]
        transfer_data = b"\x0c" + struct.pack("<Q", 5000) + b"\x06"
        transfer_ix = Instruction(
            token_prog,
            transfer_data,
            [
                AccountMeta(buyer_ata, is_signer=False, is_writable=True),
                AccountMeta(mint, is_signer=False, is_writable=False),
                AccountMeta(seller_ata, is_signer=False, is_writable=True),
                AccountMeta(buyer_pubkey, is_signer=True, is_writable=False),
            ],
        )
        # 4. Memo (16 random hex bytes = 32 ASCII bytes)
        memo_ix = Instruction(
            Pubkey.from_string(memo_prog_str),
            binascii.hexlify(os.urandom(16)),
            [],
        )

        msg = MessageV0.try_compile(
            payer=fee_payer,
            instructions=[cu_limit_ix, cu_price_ix, transfer_ix, memo_ix],
            address_lookup_table_accounts=[],
            recent_blockhash=Hash.default(),
        )
        buyer_sig = buyer_kp.sign_message(b"\x80" + bytes(msg))
        tx = VersionedTransaction.populate(msg, [Signature.default(), buyer_sig])
        tx_b64 = base64.b64encode(bytes(tx)).decode("ascii")

        requirements = PaymentRequirements(
            scheme="exact",
            network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            asset=mint_str,
            amount="5000",
            pay_to=seller_str,
            max_timeout_seconds=300,
            extra={"feePayer": fee_payer_str},
        )
        payload = PaymentPayload(
            x402_version=2,
            payload={"transaction": tx_b64},
            accepted=requirements,
            resource=ResourceInfo(
                url="https://smartfetch-production-ea53.up.railway.app/fetch",
                description="SmartFetch Web Reader",
                mime_type="application/json",
            ),
        )

        mock_signer = MagicMock()
        mock_signer.get_addresses.return_value = [fee_payer_str]
        mock_signer.verify_transaction.return_value = None
        mock_signer.simulate_transaction.return_value = None

        facilitator_scheme = ExactSvmFacilitatorScheme(signer=mock_signer)
        verify_res = facilitator_scheme.verify(payload, requirements)
        self.assertTrue(verify_res.is_valid, f"Expected valid SVM payload, got: {verify_res.invalid_reason}")
        self.assertEqual(verify_res.payer, str(buyer_pubkey))



if __name__ == "__main__":
    unittest.main()
