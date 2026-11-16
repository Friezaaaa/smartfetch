# SmartFetch Production Validation & Completion Report

**Service Name**: SmartFetch  
**Release Version**: 1.12.0 (Validation baseline: 1.11.1)  
**Operating Mode**: `isolated_browser`  
**Git Tag**: `v1.12.0`  
**Base Mainnet Settlement**: Verified  
**Audit Status**: **PRODUCTION APPROVED (GO)**  
**Date**: 2026-11-16  

---

## 1. Executive Summary

SmartFetch has undergone a comprehensive, multi-phase independent engineering and security audit followed by remediation, architectural hardening, and live end-to-end payment validation. 

All identified vulnerabilities, metadata discrepancies, and facilitator compatibility gaps have been resolved:
1. **Payment Enforcement**: The x402 payment wrapper is strictly integrated across the HTTP `/fetch` API and all four MCP tools (`fetch_webpage`, `webpage_to_markdown`, `extract_webpage_text`, and `render_webpage`). All non-payment execution paths have been eliminated.
2. **Facilitator Integration**: Coinbase CDP facilitator credentials (`CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`) are configured and authenticated against the mainnet facilitator endpoint (`https://api.cdp.coinbase.com/platform/v2/x402`), enabling on-chain settlement on **Base Mainnet** (`eip155:8453`).
3. **Execution Verification**: A real MetaMask buyer payment was completed and confirmed on-chain on Base Mainnet ([Tx `0x0137d6f3...527e`](https://basescan.org/tx/0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e)). SmartFetch verified the EIP-712 payment authorization prior to browser retrieval, rendered the requested target via the isolated DigitalOcean Chromium daemon, and settled the USDC transfer with zero discrepancy.
4. **Isolated Browser Security**: The DigitalOcean VPS browser daemon operates in zero-trust isolation, requiring mutual TLS (mTLS) with client certificates and HMAC-SHA256 signatures with nonce replay protection. Internal worker containers execute with `network_mode: none` under non-root UID 10001 with active Chromium sandboxing.

SmartFetch is **APPROVED AND READY FOR PRODUCTION** traffic.

---

## 2. Production Architecture

The SmartFetch production deployment employs a defense-in-depth, two-tier architecture separating the public MCP/REST gateway from headless browser execution:

```mermaid
graph TD
    subgraph Public Internet
        Buyer[AI Agent / Web Buyer]
        MetaMask[MetaMask Buyer Wallet]
        CDP[Coinbase CDP Facilitator]
        BaseScan[Base Mainnet - Chain 8453]
    end

    subgraph Railway Cloud
        Gateway[SmartFetch FastAPI / MCP Gateway]
        Payments[x402 Payment Middleware]
        mTLSClient[mTLS + HMAC Signing Client]
    end

    subgraph DigitalOcean VPS 159.89.40.174
        mTLSServer[mTLS Server :8443]
        HMACGuard[HMAC & Replay Verification]
        PolicyBroker[Destination SSRF Policy Broker]
        Bridge[Unix Socket Relay /ipc/relay.sock]
        subgraph Ephemeral Worker
            Docker[Docker Container --network none]
            Chromium[Chromium Headless UID 10001]
        end
    end

    Buyer -->|1. Request /fetch or MCP tool| Gateway
    Gateway -->|2. HTTP 402 Challenge| Buyer
    Buyer -->|3. Sign EIP-712| MetaMask
    MetaMask -->|4. Signed PAYMENT-SIGNATURE| Gateway
    Gateway -->|5. Verify Authorization| CDP
    CDP -->>|6. Verification OK| Gateway
    Gateway -->|7. Sign & Send Job| mTLSClient
    mTLSClient -->|8. HTTPS + mTLS + HMAC| mTLSServer
    mTLSServer --> HMACGuard
    HMACGuard --> PolicyBroker
    PolicyBroker --> Bridge
    Bridge --> Docker
    Docker --> Chromium
    Chromium -->|CDP Navigation via IPC| Bridge
    Bridge --> PolicyBroker
    PolicyBroker -->|Outbound Fetch| Public Internet
    Chromium -->>|Parsed Output| Bridge
    Bridge -->>|Result JSON| Gateway
    Gateway -->|9. Execute Settlement| CDP
    CDP -->|10. Transfer USDC| BaseScan
    BaseScan -->>|11. Tx Hash Confirmed| CDP
    CDP -->>|12. Settlement Receipt| Gateway
    Gateway -->>|13. HTTP 200 + Content + Receipt| Buyer
```

### Components
1. **Railway Service (`smartfetch`)**:
   - Public URL: `https://smartfetch-production-ea53.up.railway.app`
   - Role: Public HTTP/REST & Streamable MCP server; handles discovery, rate limiting, request bounds, and x402 payment validation.
   - Enforces `BROWSER_WORKER_REQUIRED=true`, preventing unisolated local fallback.
2. **DigitalOcean VPS (`159.89.40.174`)**:
   - Port: `8443` (Strict mTLS + HMAC)
   - Role: Isolated browser execution host.
   - Orchestrates ephemeral Docker containers (`smartfetch-browser-worker:prod`) launched per-request with dedicated Unix IPC sockets and `network_mode: none`.

---

## 3. Payment Flow & Parameter Specification

The production payment parameters are locked and verified against Base Mainnet contracts:

| Parameter | Authoritative Value | Description |
| :--- | :--- | :--- |
| **Payment Protocol** | `x402` (Version 2) | Standard HTTP 402 challenge & signature protocol |
| **Payment Network** | `eip155:8453` | Base Mainnet |
| **Payment Scheme** | `exact` | EIP-3009 `TransferWithAuthorization` / EIP-712 typed signing |
| **Payment Asset** | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | Native USD Coin (USDC) on Base |
| **Seller Recipient (`payTo`)** | `0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786` | SmartFetch production treasury address |
| **Fixed Price** | `5000` atomic units ($0.005 USDC) | Fixed cost per successful page retrieval |
| **Facilitator Host** | `https://api.cdp.coinbase.com/platform/v2/x402` | Coinbase Developer Platform Mainnet Facilitator |
| **Execution Protection** | Strict Fail-Closed | Settlement is executed **only** after successful retrieval |

---

## 4. Security Controls & Audit Verification

| Control Area | Security Specification | Audit Status | Evidence |
| :--- | :--- | :--- | :--- |
| **Mutual TLS (mTLS)** | Client certificate signed by private CA required on VPS :8443 | **PASSED** | Handshake without cert rejected at TLS layer; untrusted cert rejected (`SSLError`). |
| **Cryptographic HMAC** | HMAC-SHA256 signature calculated over `METHOD\nPATH\nTIMESTAMP\nNONCE\nBODY_HASH` | **PASSED** | Unsigned and tampered requests rejected with `401 Unauthorized`. |
| **Replay Protection** | 16-byte cryptographically secure nonce with 300s timestamp sliding window | **PASSED** | Replayed nonce rejected with `401 Unauthorized` (`Nonce already used`). |
| **SSRF Destination Policy** | Broker blocks `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254` | **PASSED** | Metadata and loopback URLs rejected with `403 Policy Rejection`. |
| **Container Isolation** | Ephemeral Docker containers run with `--network none` and `--user 10001:10001` | **PASSED** | Outbound access possible only through Unix IPC broker relay. |
| **Chromium Sandboxing** | Standard setuid/namespace sandboxing active; non-root user execution | **PASSED** | Zero `--no-sandbox` flags in worker runtime; verified non-root. |
| **Free Bypass Prevention** | All 4 MCP tools and `/fetch` API route enforce x402 payment validation | **PASSED** | Free retrieval attempts return HTTP 402 with valid challenge header. |
| **Resource Cleanup** | Ephemeral container removed immediately after execution | **PASSED** | VPS reports `active_jobs: 0`, `capacity: 2` post-execution. |

---

## 5. Live Paid Flow Validation Evidence

A live end-to-end regression validation was performed using a real buyer wallet in MetaMask:

### Transaction Confirmation
- **Buyer Address**: `0x043bd200d0c2379c06a8cf64d222b14cf7654ae7`
- **Seller Address**: `0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786`
- **USDC Contract**: `0x833589fcd6edb6e08f4c7c32d4f71b54bda02913`
- **Transaction Hash**: [`0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e`](https://basescan.org/tx/0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e)
- **Base Block**: `58093411`
- **On-Chain Events**:
  - `AuthorizationUsed(authorizer=0x043bd200..., nonce=0x951f7abf...)`
  - `Transfer(from=0x043bd200..., to=0xe29fcb6e..., value=5000)` ($0.005 USDC)

### Retrieval Output & Header Receipt
- **HTTP Status**: `200 OK`
- **Elapsed Execution Time**: `5573 ms`
- **Render Method**: `browser` (executed via DigitalOcean VPS)
- **Target Page**: `https://example.com/` (`Example Domain`)
- **Payment-Response Header**:
  ```json
  {
    "success": true,
    "payer": "0x043bd200d0c2379c06a8cf64d222b14cf7654ae7",
    "transaction": "0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e",
    "network": "eip155:8453"
  }
  ```

---

## 6. Operational Readiness & Health Assessment

All production health and discovery endpoints are operational:

- **Health Endpoint (`/health`)**:
  ```json
  {
    "ok": true,
    "service": "SmartFetch",
    "version": "1.12.0",
    "retrieval_available": true,
    "browser_available": true,
    "mode": "isolated_browser"
  }
  ```
- **Metadata Endpoint (`/meta`)**: Reports `isolated_browser` mode with all 4 MCP tools and active payment challenges.
- **Manifest Endpoint (`/.well-known/x402`)**: Returns compliant x402 manifest with Base Mainnet USDC specifications.
- **VPS Browser Daemon (`https://159.89.40.174:8443/health`)**:
  ```json
  {
    "ok": true,
    "service": "smartfetch-browser-worker",
    "version": "1.12.0",
    "active_jobs": 0,
    "capacity": 2
  }
  ```
