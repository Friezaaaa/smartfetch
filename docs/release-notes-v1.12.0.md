# SmartFetch v1.12.0 Release Notes

**Release**: `v1.12.0`  
**Date**: 2026-11-16  
**Status**: Production Release  

---

## Highlights

SmartFetch v1.12.0 transitions the service from maintenance into full production with an isolated, zero-trust browser rendering architecture and verified x402 payment enforcement on Base Mainnet.

### Key Capabilities

1. **Zero-Trust VPS Browser Isolation**:
   - Webpage rendering is offloaded from the public gateway to an isolated DigitalOcean VPS daemon (`159.89.40.174:8443`).
   - Mutual TLS (mTLS) with client certificate verification and HMAC-SHA256 signature authentication with nonce replay protection.
   - Outbound browser execution runs inside ephemeral Docker containers under `network_mode: none` and non-root UID 10001, mediated exclusively by a policy broker that blocks SSRF access to loopback and cloud metadata endpoints (`169.254.169.254`).
   - Native Chromium sandboxing active with zero `--no-sandbox` usage.

2. **Base Mainnet x402 Payment Enforcement**:
   - All four MCP tools (`fetch_webpage`, `webpage_to_markdown`, `extract_webpage_text`, `render_webpage`) and the REST `/fetch` API require valid x402 payment before execution.
   - Fixed price of **$0.005 USDC** (`5000` atomic units) on **Base Mainnet** (`eip155:8453`).
   - EIP-712 / EIP-3009 transfer authorizations verified and settled via authenticated Coinbase Developer Platform (CDP) mainnet facilitator.
   - Strict fail-closed settlement guarantee: payment settlement is only initiated upon confirmed, successful page extraction.

3. **Production Metadata & Discovery**:
   - Verified OpenAPI 3.1.0 specifications (`/openapi.json`), LLM documentation (`/llms.txt`), and x402 bazaar discovery manifests (`/.well-known/x402`).

---

## On-Chain Verification

The end-to-end paid workflow was verified live with MetaMask on Base Mainnet:
- **Transaction Hash**: [`0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e`](https://basescan.org/tx/0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e)
- **Settlement Asset**: USDC (`0x833589fcd6edb6e08f4c7c32d4f71b54bda02913`)
- **Recipient**: `0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786`
- **Render Latency**: 5.57 seconds
