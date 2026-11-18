# SmartFetch V1.15.0 — Production Isolated Browser & x402 Payments

SmartFetch provides reliable, high-performance public webpage retrieval and rendering for AI agents and developer applications with zero-trust browser isolation and x402 micropayment protection.

---

## Live Interactive Playground

Explore SmartFetch directly from your browser without installing anything:
- **Primary Playground (Cloudflare Pages)**: [https://smartfetch-playground.pages.dev](https://smartfetch-playground.pages.dev)
- **GitHub Pages Mirror**: [https://friezaaaa.github.io/smartfetch/](https://friezaaaa.github.io/smartfetch/)

**Features**:
- **Free Live 402 Inspection**: Decode and inspect the live dual-chain Base Mainnet (`eip155:8453`) and Solana Mainnet (`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`) x402 challenge header, pricing ($0.005 / $0.01 / $0.015 USDC), seller treasury addresses, and service health without a wallet.
- **Dual-Chain Web3 Wallet Payments (Base + Solana)**: Sign EIP-712 `TransferWithAuthorization` authorizations via MetaMask, Coinbase Wallet, or Rabby on Base Mainnet (`0x2105` / `8453`) OR sign SPL `TransferChecked` transactions via Solana Wallet Standard (Phantom, Solflare, Backpack, MetaMask Solana) on Solana Mainnet with facilitator-sponsored gas.
- **HTML DOM + Local PDF Support**: Interactive previews featuring sandboxed HTML rendering (`sandbox="allow-scripts"`), formatted Markdown, extracted plain text, and local PDF extraction.
- **Client-Side SSRF Protection**: Real-time validation preventing requests to internal IPs, localhost, or cloud metadata endpoints (`169.254.169.254`).
- **Interactive Code Generator**: Ready-to-copy integration code in cURL, Python (`x402`), TypeScript (`viem` / `@solana/web3.js`), and MCP configuration JSON.

---

## Apify Store (Cards, PayPal, Standby MCP)

Prefer traditional cloud billing (credit/debit cards, PayPal, or prepaid Apify credits) instead of on-chain crypto wallets? SmartFetch is available as a self-contained **Pay-Per-Event (PPE) Actor** and **Standby MCP Server** on the Apify Store:

- **Public Apify Store Actor**: [https://apify.com/smartfetch.dev/smartfetch](https://apify.com/smartfetch.dev/smartfetch)
- **Standby MCP Endpoint**: `https://smartfetch-dev--smartfetch.apify.actor/mcp` (pass `Authorization: Bearer <APIFY_API_TOKEN>`)
- **Bulk B2B Lead & Tech-Stack Enrichment (`bulk_inspect`)**: Supply up to 50 bare domains or URLs (`["stripe.com", "linear.app", ...]`) to concurrently extract flat, CSV/Excel-ready rows containing `domain`, `url`, `title`, `description`, `emails`, `phones`, `github`, `linkedin`, `x_twitter`, `tech_stack`, `https`, `has_hsts`, and `has_csp`.
- **Site URL Discovery (`map` / `map_website`)**: Discover up to 500 internal URLs on any website via `robots.txt` (`Sitemap:`), `/sitemap.xml` (including nested `<sitemapindex>`), and internal `<a href>` links with optional keyword filtering.
- **Pay-Per-Event Pricing**:
  - `actor-start`: `$0.00` (no startup fee; `$0.00001` platform minimum)
  - `page-fetched`: **`$0.01`** per successfully retrieved webpage, PDF, or site map (`fetch`, `batch_fetch`, & `map`)
  - `site-inspected`: **`$0.01`** per successful structured site intelligence or B2B lead enrichment (`inspect` & `bulk_inspect`)
  - `screenshot-captured`: **`$0.02`** per successful headless Chromium screenshot (`screenshot`)
  - Zero charge on failed requests.

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

- **Service Version**: `1.15.0`
- **Operating Mode**: `isolated_browser`
- **Supported Networks**:
  - **Base Mainnet** (`eip155:8453`) — Native USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
  - **Solana Mainnet** (`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`) — Native SPL USDC (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`)
- **Price**: $0.005 USDC (5000 atomic units) for webpage retrieval, site inspection, and site URL mapping (`POST /fetch`, `POST /inspect`, `POST /map`, retrieval tools, `inspect_webpage`, and `map_website`); $0.01 USDC (10000 atomic units) for screenshot capture (`POST /screenshot` and `screenshot_webpage`); $0.015 USDC (15000 atomic units) for parallel batch retrieval (`POST /batch-fetch` and `batch_fetch_webpages`)
- **Seller Treasury (`payTo`)**:
  - **Base (EVM)**: `0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786`
  - **Solana (SVM)**: `88hE4EB4yZwb4F2arPXcG7efwQyU5FDN26DuVpVKVk2W`
- **MCP Tools**:
  - `fetch_webpage` ($0.005 USDC)
  - `webpage_to_markdown` ($0.005 USDC)
  - `extract_webpage_text` ($0.005 USDC)
  - `render_webpage` ($0.005 USDC)
  - `screenshot_webpage` ($0.01 USDC)
  - `inspect_webpage` ($0.005 USDC)
  - `batch_fetch_webpages` ($0.015 USDC)
  - `map_website` ($0.005 USDC)
- **Opt-In Endpoints (enabled when provider keys are configured on server)**:
  - `POST /search` / `search_web` ($0.010 USDC; requires `SERPER_API_KEY` or `EXA_API_KEY`)
  - `POST /search-and-fetch` / `search_and_fetch` ($0.020 USDC; requires `SERPER_API_KEY` or `EXA_API_KEY`)
  - `POST /leads` / `search_leads` ($0.030 USDC; requires `SERPER_API_KEY`; also available as `search_leads` on Apify)
  - `POST /x-search` / `search_x_posts` ($0.008 USDC; requires `TWITTERAPI_KEY`)
  - `map_website` ($0.005 USDC)
- **REST Endpoints**:
  - `POST /fetch` ($0.005 USDC)
  - `POST /screenshot` ($0.01 USDC)
  - `POST /inspect` ($0.005 USDC)
  - `POST /batch-fetch` ($0.015 USDC)
  - `POST /map` ($0.005 USDC)

---

## Documentation & Validation

- **Agent Integration & Payment Guide**: [`docs/agent-payment-guide.md`](docs/agent-payment-guide.md) — Complete Python, TypeScript, and MCP client examples for Base Mainnet EIP-712 payment authorization.
- **Production Traffic Analysis**: [`docs/production-traffic-analysis-v1.12.0.md`](docs/production-traffic-analysis-v1.12.0.md) — Telemetry audit of AI agent discovery, MCP indexer traffic, and verified settlements.
- **Playground Architecture Plan**: [`docs/playground-architecture-plan.md`](docs/playground-architecture-plan.md) — Design and security specification for the interactive web playground.
- **Production Audit & Verification**: [`docs/production-validation.md`](docs/production-validation.md) — Verified on-chain Base Mainnet settlement and mTLS isolated browser audit.
- **Release Notes**: [`docs/release-notes-v1.12.0.md`](docs/release-notes-v1.12.0.md)
- **VPS Deployment Guide**: [`docs/vps_deployment_guide.md`](docs/vps_deployment_guide.md)

---

## Agent Quickstart & x402 Micropayments

SmartFetch enables autonomous AI agents to pay for web retrievals using off-chain EIP-712 payment authorizations settled on Base Mainnet.

### 5-Step Agent Workflow
1. **Discover**: Query `/.well-known/x402` or `/.well-known/agent-card.json` to verify pricing ($0.005 USDC on Base Mainnet).
2. **Challenge**: Submit an unauthenticated `POST /fetch` request; SmartFetch replies with `HTTP 402 Payment Required`.
3. **Sign**: Agent inspects the `PAYMENT-REQUIRED` header, checks its spending limit, and signs an EIP-712 `TransferWithAuthorization`.
4. **Execute**: Resubmit `POST /fetch` with the signature in the `PAYMENT-SIGNATURE` header.
5. **Retrieve**: SmartFetch verifies the authorization, renders the page inside the DigitalOcean isolated container, settles the payment on Base Mainnet, and returns HTTP 200 with the transaction receipt in `PAYMENT-RESPONSE`.

### Discovery Manifests
| Endpoint | Standard | Description |
| :--- | :--- | :--- |
| `GET /.well-known/x402` | x402 Manifest v2 | Machine-readable pricing ($0.005), USDC asset address, and payment scheme |
| `GET /.well-known/agent-card.json` | A2A Protocol | Agent-to-Agent discovery manifest (capabilities, MCP endpoints, contact) |
| `GET /.well-known/owners.json` | VerifyMCP Standard | Verified domain and MCP identity proof |
| `GET /mcp` | Model Context Protocol | Streamable HTTP transport for AI agent tool invocation |
| `GET /openapi.json` | OpenAPI 3.1 | REST API schema for automated code generation |
| `GET /llms.txt` | LLMs.txt | LLM-readable summary and prompt engineering reference |
