# SmartFetch V1.12.0 — Production Isolated Browser & x402 Payments

SmartFetch provides reliable, high-performance public webpage retrieval and rendering for AI agents and developer applications with zero-trust browser isolation and x402 micropayment protection.

---

## Architecture Overview

SmartFetch separates the public API/MCP interface from headless browser execution across two isolated tiers:

1. **Railway Public Gateway**:
   - Hosts the streamable Model Context Protocol (MCP) server (`/mcp`) and REST API (`/fetch`).
   - Serves discovery documents: `/.well-known/x402`, `/openapi.json`, `/llms.txt`, `/meta`, `/docs`.
   - Strictly enforces x402 micropayment challenges across all retrieval operations ($0.005 USDC on Base Mainnet).
   - Verifies and settles EIP-712 payment authorizations via Coinbase Developer Platform (CDP) facilitators.

2. **DigitalOcean Isolated Browser Worker (VPS)**:
   - Dedicated headless browser rendering host communicating with Railway via mutual TLS (mTLS) and HMAC-SHA256 signature verification.
   - Replay protection with 16-byte nonce validation and sliding timestamp window.
   - Renders web pages inside ephemeral Docker containers (`--network none`, non-root UID `10001:10001`).
   - All network traffic is mediated by an isolated Unix IPC policy broker enforcing strict SSRF protections against private IP ranges and cloud metadata endpoints (`169.254.169.254`).

---

## Production Parameters

- **Service Version**: `1.12.0`
- **Operating Mode**: `isolated_browser`
- **Supported Network**: Base Mainnet (`eip155:8453`)
- **Asset**: Native USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
- **Price**: 5000 atomic units ($0.005 USDC) per successful request
- **Seller Treasury (`payTo`)**: `0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786`
- **MCP Tools**:
  - `fetch_webpage`
  - `webpage_to_markdown`
  - `extract_webpage_text`
  - `render_webpage`

---

## Documentation & Validation

- Full production audit and on-chain verification report: [`docs/production-validation.md`](docs/production-validation.md)
- Release notes: [`docs/release-notes-v1.12.0.md`](docs/release-notes-v1.12.0.md)
- VPS deployment guide: [`docs/vps_deployment_guide.md`](docs/vps_deployment_guide.md)
