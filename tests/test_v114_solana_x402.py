"""Tests for optional multi-network Solana USDC (x402 SVM) payment support."""

import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from x402.http.utils import decode_payment_required_header
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import server
from smartfetch.discovery import (
    agent_card_document,
    docs_html,
    llms_text,
    openapi_document,
    public_urls,
    x402_manifest,
)
from smartfetch.mcp_server import create_smartfetch_mcp
from smartfetch.payments import (
    BASE_MAINNET,
    BASE_SEPOLIA,
    SOLANA_DEVNET,
    SOLANA_MAINNET,
    X402Settings,
    build_payment_options,
    create_x402_resource_server,
    install_x402,
    load_x402_settings,
)

VALID_EVM_ADDRESS = "0x1111111111111111111111111111111111111111"
VALID_SOLANA_ADDRESS = "9vPA6119v4293f9WkEegCjG1y1f19pW6BwBvL9eB6e6V"
FEE_PAYER_SOLANA = "CKPKJWNdJEqa81x7CkZ14BVPiY6y16Sxs7owznqtWYp5"


def mock_dual_supported(evm_net=BASE_MAINNET, svm_net=SOLANA_MAINNET):
    return SupportedResponse(
        kinds=[
            SupportedKind(x402Version=2, scheme="exact", network=evm_net),
            SupportedKind(
                x402Version=2,
                scheme="exact",
                network=svm_net,
                extra={"feePayer": FEE_PAYER_SOLANA},
            ),
        ]
    )


class SolanaPaymentSettingsTests(unittest.TestCase):
    def test_default_without_solana_env(self):
        settings = load_x402_settings({
            "X402_ENABLED": "true",
            "X402_PAY_TO": VALID_EVM_ADDRESS,
        })
        self.assertTrue(settings.enabled)
        self.assertIsNone(settings.solana_pay_to)
        self.assertIsNone(settings.solana_network)

    def test_auto_selects_solana_mainnet_with_base_mainnet(self):
        settings = load_x402_settings({
            "X402_ENABLED": "true",
            "X402_PAY_TO": VALID_EVM_ADDRESS,
            "X402_NETWORK": BASE_MAINNET,
            "X402_SOLANA_PAY_TO": VALID_SOLANA_ADDRESS,
            "X402_ALLOW_MOCK_FACILITATOR": "true",
        })
        self.assertEqual(settings.solana_pay_to, VALID_SOLANA_ADDRESS)
        self.assertEqual(settings.solana_network, SOLANA_MAINNET)

    def test_auto_selects_solana_devnet_with_base_sepolia(self):
        settings = load_x402_settings({
            "X402_ENABLED": "true",
            "X402_PAY_TO": VALID_EVM_ADDRESS,
            "X402_NETWORK": BASE_SEPOLIA,
            "X402_SOLANA_PAY_TO": VALID_SOLANA_ADDRESS,
        })
        self.assertEqual(settings.solana_pay_to, VALID_SOLANA_ADDRESS)
        self.assertEqual(settings.solana_network, SOLANA_DEVNET)

    def test_explicit_solana_network_override(self):
        settings = load_x402_settings({
            "X402_ENABLED": "true",
            "X402_PAY_TO": VALID_EVM_ADDRESS,
            "X402_NETWORK": BASE_SEPOLIA,
            "X402_SOLANA_PAY_TO": VALID_SOLANA_ADDRESS,
            "X402_SOLANA_NETWORK": SOLANA_MAINNET,
        })
        self.assertEqual(settings.solana_network, SOLANA_MAINNET)

    def test_rejects_invalid_solana_network(self):
        with self.assertRaisesRegex(ValueError, "X402_SOLANA_NETWORK"):
            load_x402_settings({
                "X402_ENABLED": "true",
                "X402_PAY_TO": VALID_EVM_ADDRESS,
                "X402_SOLANA_PAY_TO": VALID_SOLANA_ADDRESS,
                "X402_SOLANA_NETWORK": "solana:unsupported_network",
            })

    def test_rejects_evm_address_as_solana_pay_to(self):
        with self.assertRaisesRegex(ValueError, "X402_SOLANA_PAY_TO"):
            load_x402_settings({
                "X402_ENABLED": "true",
                "X402_PAY_TO": VALID_EVM_ADDRESS,
                "X402_SOLANA_PAY_TO": VALID_EVM_ADDRESS,
            })

    def test_rejects_invalid_base58_solana_pay_to(self):
        for invalid in ("not-base-58-with-0-or-O", "short", "9" * 60, "???"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "X402_SOLANA_PAY_TO"
            ):
                load_x402_settings({
                    "X402_ENABLED": "true",
                    "X402_PAY_TO": VALID_EVM_ADDRESS,
                    "X402_SOLANA_PAY_TO": invalid,
                })

    def test_rejects_all_ones_system_address(self):
        with self.assertRaisesRegex(ValueError, "X402_SOLANA_PAY_TO"):
            load_x402_settings({
                "X402_ENABLED": "true",
                "X402_PAY_TO": VALID_EVM_ADDRESS,
                "X402_SOLANA_PAY_TO": "11111111111111111111111111111111",
            })


class SolanaPaymentOptionsHelperTests(unittest.TestCase):
    def test_build_payment_options_single_without_solana(self):
        settings = X402Settings(
            enabled=True,
            pay_to=VALID_EVM_ADDRESS,
            price="$0.005",
            network=BASE_MAINNET,
        )
        opts = build_payment_options(settings, "$0.005")
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0].network, BASE_MAINNET)
        self.assertEqual(opts[0].pay_to, VALID_EVM_ADDRESS)

    def test_build_payment_options_dual_with_solana(self):
        settings = X402Settings(
            enabled=True,
            pay_to=VALID_EVM_ADDRESS,
            price="$0.005",
            network=BASE_MAINNET,
            solana_pay_to=VALID_SOLANA_ADDRESS,
            solana_network=SOLANA_MAINNET,
        )
        opts = build_payment_options(settings, "$0.01")
        self.assertEqual(len(opts), 2)
        self.assertEqual(opts[0].network, BASE_MAINNET)
        self.assertEqual(opts[0].pay_to, VALID_EVM_ADDRESS)
        self.assertEqual(opts[0].price, "$0.01")
        self.assertEqual(opts[1].network, SOLANA_MAINNET)
        self.assertEqual(opts[1].pay_to, VALID_SOLANA_ADDRESS)
        self.assertEqual(opts[1].price, "$0.01")


class SolanaLiveAndAppTests(unittest.TestCase):
    @patch("x402.http.HTTPFacilitatorClient.get_supported")
    def test_dual_chain_402_challenges_on_all_four_routes(self, get_supported):
        get_supported.return_value = mock_dual_supported(BASE_MAINNET, SOLANA_MAINNET)

        settings = X402Settings(
            enabled=True,
            pay_to=VALID_EVM_ADDRESS,
            price="$0.005",
            network=BASE_MAINNET,
            solana_pay_to=VALID_SOLANA_ADDRESS,
            solana_network=SOLANA_MAINNET,
        )

        with patch.dict("os.environ", {
            "BROWSER_WORKER_URL": "https://159.89.40.174:9443",
            "BROWSER_WORKER_HMAC_KEY": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        }):
            app = server.create_app(settings)
            with TestClient(app) as client:
                endpoints = [
                    ("/fetch", {"url": "https://example.com/"}, "5000"),
                    ("/screenshot", {"url": "https://example.com/"}, "10000"),
                    ("/inspect", {"url": "https://example.com/"}, "5000"),
                    ("/batch-fetch", {"urls": ["https://example.com/"]}, "15000"),
                ]

                for path, body, expected_atomic in endpoints:
                    with self.subTest(endpoint=path):
                        res = client.post(path, json=body)
                        self.assertEqual(res.status_code, 402, f"{path} did not return 402: {res.text}")
                        header = res.headers.get("PAYMENT-REQUIRED")
                        self.assertIsNotNone(header, f"Missing PAYMENT-REQUIRED header on {path}")

                        challenge = decode_payment_required_header(header)
                        self.assertEqual(len(challenge.accepts), 2, f"{path} challenge did not have 2 options")

                        evm_opt = next(opt for opt in challenge.accepts if opt.network == BASE_MAINNET)
                        sol_opt = next(opt for opt in challenge.accepts if opt.network == SOLANA_MAINNET)

                        self.assertEqual(evm_opt.pay_to, VALID_EVM_ADDRESS)
                        self.assertEqual(evm_opt.amount, expected_atomic)
                        self.assertEqual(sol_opt.pay_to, VALID_SOLANA_ADDRESS)
                        self.assertEqual(sol_opt.amount, expected_atomic)
                        self.assertIn("feePayer", sol_opt.extra)
                        self.assertEqual(sol_opt.extra["feePayer"], FEE_PAYER_SOLANA)

    @patch("x402.http.HTTPFacilitatorClient.get_supported")
    def test_mcp_tools_dual_requirements_in_accepts(self, get_supported):
        get_supported.return_value = mock_dual_supported(BASE_MAINNET, SOLANA_MAINNET)

        settings = X402Settings(
            enabled=True,
            pay_to=VALID_EVM_ADDRESS,
            price="$0.005",
            network=BASE_MAINNET,
            solana_pay_to=VALID_SOLANA_ADDRESS,
            solana_network=SOLANA_MAINNET,
        )

        async def dummy_fetch(*args, **kwargs):
            return {"success": True}

        mcp_obj = create_smartfetch_mcp(settings, dummy_fetch)
        self.assertIsNotNone(mcp_obj.resource_server)
        self.assertEqual(len(mcp_obj.accepts), 2)
        evm_accept = next(req for req in mcp_obj.accepts if req.network == BASE_MAINNET)
        sol_accept = next(req for req in mcp_obj.accepts if req.network == SOLANA_MAINNET)
        self.assertEqual(evm_accept.pay_to, VALID_EVM_ADDRESS)
        self.assertEqual(sol_accept.pay_to, VALID_SOLANA_ADDRESS)


class SolanaDiscoveryTests(unittest.TestCase):
    def setUp(self):
        class MockRequest:
            class URL:
                scheme = "https"
                netloc = "smartfetch-production-ea53.up.railway.app"
            url = URL()
        self.urls = public_urls(MockRequest())

    def test_discovery_with_solana_enabled(self):
        settings = X402Settings(
            enabled=True,
            pay_to=VALID_EVM_ADDRESS,
            price="$0.005",
            network=BASE_MAINNET,
            solana_pay_to=VALID_SOLANA_ADDRESS,
            solana_network=SOLANA_MAINNET,
        )

        # 1. x402_manifest
        manifest = x402_manifest(self.urls, settings)
        self.assertEqual(manifest["payment"]["network"], BASE_MAINNET)
        self.assertEqual(manifest["payment"]["payTo"], VALID_EVM_ADDRESS)
        self.assertIn("solana", manifest["payment"])
        self.assertEqual(manifest["payment"]["solana"]["network"], SOLANA_MAINNET)
        self.assertEqual(manifest["payment"]["solana"]["payTo"], VALID_SOLANA_ADDRESS)
        self.assertEqual(manifest["payment"]["networks"], [BASE_MAINNET, SOLANA_MAINNET])

        # 2. agent_card_document
        agent_card = agent_card_document(self.urls, settings)
        x402_proto = agent_card["protocols"]["x402"]
        self.assertEqual(x402_proto["network"], BASE_MAINNET)
        self.assertEqual(x402_proto["payTo"], VALID_EVM_ADDRESS)
        self.assertIn("solana", x402_proto)
        self.assertEqual(x402_proto["solana"]["network"], SOLANA_MAINNET)
        self.assertEqual(x402_proto["solana"]["payTo"], VALID_SOLANA_ADDRESS)
        self.assertEqual(x402_proto["networks"], [BASE_MAINNET, SOLANA_MAINNET])

        # 3. openapi_document
        openapi = openapi_document(self.urls, settings)
        fetch_op = openapi["paths"]["/fetch"]["post"]
        self.assertEqual(fetch_op["x-x402"]["network"], BASE_MAINNET)
        self.assertIn("solana", fetch_op["x-x402"])
        self.assertEqual(fetch_op["x-x402"]["solana"]["network"], SOLANA_MAINNET)
        self.assertEqual(fetch_op["x-x402"]["solana"]["payTo"], VALID_SOLANA_ADDRESS)
        self.assertEqual(fetch_op["x-x402"]["networks"], [BASE_MAINNET, SOLANA_MAINNET])

        screenshot_op = openapi["paths"]["/screenshot"]["post"]
        self.assertIn("solana", screenshot_op["x-x402"])
        self.assertEqual(screenshot_op["x-x402"]["solana"]["payTo"], VALID_SOLANA_ADDRESS)

        inspect_op = openapi["paths"]["/inspect"]["post"]
        self.assertIn("solana", inspect_op["x-x402"])
        self.assertEqual(inspect_op["x-x402"]["solana"]["payTo"], VALID_SOLANA_ADDRESS)

        batch_op = openapi["paths"]["/batch-fetch"]["post"]
        self.assertIn("solana", batch_op["x-x402"])
        self.assertEqual(batch_op["x-x402"]["solana"]["payTo"], VALID_SOLANA_ADDRESS)

        # 4. docs_html & llms_text
        html = docs_html(self.urls, settings)
        self.assertIn(SOLANA_MAINNET, html)
        self.assertIn(VALID_SOLANA_ADDRESS, html)

        llms = llms_text(self.urls, settings)
        self.assertIn(SOLANA_MAINNET, llms)
        self.assertIn(VALID_SOLANA_ADDRESS, llms)

    def test_discovery_without_solana_remains_clean(self):
        settings = X402Settings(
            enabled=True,
            pay_to=VALID_EVM_ADDRESS,
            price="$0.005",
            network=BASE_MAINNET,
        )

        manifest = x402_manifest(self.urls, settings)
        self.assertNotIn("solana", manifest["payment"])
        self.assertNotIn("networks", manifest["payment"])

        agent_card = agent_card_document(self.urls, settings)
        self.assertNotIn("solana", agent_card["protocols"]["x402"])
        self.assertNotIn("networks", agent_card["protocols"]["x402"])

        openapi = openapi_document(self.urls, settings)
        self.assertNotIn("solana", openapi["paths"]["/fetch"]["post"]["x-x402"])
        self.assertNotIn("networks", openapi["paths"]["/fetch"]["post"]["x-x402"])

        html = docs_html(self.urls, settings)
        self.assertNotIn("solana", html.lower())

        llms = llms_text(self.urls, settings)
        self.assertNotIn("solana", llms.lower())


if __name__ == "__main__":
    unittest.main()
