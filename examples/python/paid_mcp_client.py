"""Pay for one SmartFetch MCP call with a guarded CDP-managed wallet."""

import asyncio
import json
import os


MCP_URL = os.getenv(
    "SMARTFETCH_MCP_URL",
    "https://smartfetch-production-ea53.up.railway.app/mcp",
)
TARGET_URL = os.getenv("SMARTFETCH_TARGET_URL", "https://example.com/")
BASE_MAINNET = "eip155:8453"
MAX_PAYMENT = "$0.005"


def build_payment_client(signer):
    """Build an x402 client that refuses payments above half a cent."""
    from x402 import x402Client
    from x402.mechanisms.evm.exact import ExactEvmScheme

    payment = x402Client()
    payment.register(BASE_MAINNET, ExactEvmScheme(signer))
    payment.set_spend_controls({
        "max_amount_per_payment": MAX_PAYMENT,
    })
    return payment


def _print_result(result) -> None:
    texts = [
        item.text
        for item in result.content
        if hasattr(item, "text")
    ]
    for text in texts:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            print(text)
        else:
            print(json.dumps(parsed, indent=2))

    receipt = result.payment_response
    if hasattr(receipt, "model_dump"):
        receipt = receipt.model_dump(by_alias=True, exclude_none=True)
    print("Payment made:", result.payment_made)
    print("Settlement receipt:", json.dumps(receipt, indent=2, default=str))


async def main() -> None:
    from cdp import CdpClient
    from cdp.evm_local_account import EvmLocalAccount
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from x402.mcp import x402MCPSession
    from x402.mechanisms.evm import EthAccountSigner

    async with CdpClient() as cdp:
        account = await cdp.evm.get_or_create_account(
            name="smartfetch-buyer",
        )
        signer = EthAccountSigner(EvmLocalAccount(account))
        payment = build_payment_client(signer)
        print("Paying from:", signer.address)
        print("Maximum payment:", MAX_PAYMENT, "USDC on Base mainnet")

        async with streamable_http_client(MCP_URL) as (
            read_stream,
            write_stream,
            _session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                mcp = x402MCPSession(session, payment, auto_payment=True)
                await mcp.initialize()

                listed = await mcp.list_tools()
                print("Free tools/list:", [tool.name for tool in listed.tools])
                print("Calling fetch_webpage; this can spend real USDC.")
                result = await mcp.call_tool(
                    "fetch_webpage",
                    {
                        "url": TARGET_URL,
                        "max_chars": 20000,
                        "force_browser": False,
                    },
                )
                _print_result(result)


if __name__ == "__main__":
    asyncio.run(main())
