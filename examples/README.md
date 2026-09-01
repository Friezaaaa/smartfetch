# SmartFetch paying MCP clients

These examples connect to SmartFetch over MCP Streamable HTTP, list the four
tools for free, call `fetch_webpage`, handle the x402 challenge, pay, retry the
tool call, and print the result plus settlement receipt.

**Running either example can spend real USDC on Base mainnet.** Both cap one
payment at exactly `$0.005`. The TypeScript example also caps cumulative spend
to `$0.005` per 24 hours. Fund only the printed CDP wallet address and use a
separate low-balance wallet for agent testing.

Set these CDP variables in secret storage, never in source control:

```text
CDP_API_KEY_ID=...
CDP_API_KEY_SECRET=...
CDP_WALLET_SECRET=...
```

Optional inputs:

```text
SMARTFETCH_MCP_URL=https://smartfetch-production-ea53.up.railway.app/mcp
SMARTFETCH_TARGET_URL=https://example.com/
```

## Python

From the repository root:

```bash
python -m pip install "cdp-sdk==1.47.1" "mcp==1.29.0" "x402[evm,mcp]==2.20.0"
python examples/python/paid_mcp_client.py
```

The Python example uses a CDP-managed server wallet, the standard x402 client,
and an enforced `$0.005` `max_amount_per_payment` control.

## TypeScript

```bash
cd examples/typescript
npm install
npm run check
npm start
```

`npm run check` only type-checks the example and does not connect or pay.
`npm start` provisions or reuses the CDP-managed wallet and can make the real
payment. The first run prints the Base address to fund if it has no USDC.

The examples follow the official CDP/x402 MCP loop: the first tool call is
unpaid, SmartFetch returns `PaymentRequired`, the client signs within its spend
limit, retries with `_meta["x402/payment"]`, and receives settlement details in
`_meta["x402/payment-response"]`.

For the HTTP flow, use the tested
[Python HTTP buyer](../scripts/paid_fetch_mainnet_test.py) or follow the
[official x402 TypeScript/Python buyer guide](https://docs.x402.org/getting-started/quickstart-for-buyers).
The client sends an unpaid request, validates `PAYMENT-REQUIRED`, signs within
its cap, retries with `PAYMENT-SIGNATURE`, and reads `PAYMENT-RESPONSE` after a
successful settlement.

Never place a private key or recovery phrase in a URL, request body, log,
example, command-line argument, or repository file. Never commit secret-bearing
`.env` files. Use hidden interactive input, a platform-injected secret, or an
approved wallet/secret-management service.
