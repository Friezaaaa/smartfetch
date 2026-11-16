# SmartFetch Roadmap & Production Snapshot

**Stable Baseline**: `v1.12.0`  
**Status**: Signed Off for Production  
**Baseline Commit**: `6a6be0905cf70b566498313520991c368f1deea7`  
**Base Mainnet Verified Transaction**: [`0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e`](https://basescan.org/tx/0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e)  

---

## 1. Executive Summary & Baseline Architecture

SmartFetch v1.12.0 represents the frozen, validated production baseline. The core architecture isolates public API and payment mediation from headless browser execution across two distinct trust boundaries:

```
+-------------------------------------------------------------------------------+
|                             PUBLIC CLIENTS / AGENTS                           |
|                       (MetaMask, AI Agents, MCP Hosts)                        |
+-------------------------------------------------------------------------------+
                                        |  HTTPS (REST /fetch, MCP /mcp)
                                        |  x402 Micropayment: $0.005 USDC
                                        v
+-------------------------------------------------------------------------------+
|                          RAILWAY PUBLIC GATEWAY (v1.12.0)                     |
|  - Streamable MCP Server & REST API endpoints                                  |
|  - Public Discovery: /.well-known/x402, /openapi.json, /llms.txt, /meta       |
|  - Strict x402 payment challenge & verification via Coinbase CDP Platform     |
|  - Fail-closed settlement guarantee (settle only after successful retrieval)  |
+-------------------------------------------------------------------------------+
                                        |
                                        |  Mutual TLS (mTLS) + HMAC-SHA256
                                        |  Replay Protection (16-byte nonce, timestamps)
                                        v
+-------------------------------------------------------------------------------+
|                       DIGITALOCEAN BROWSER WORKER (VPS)                       |
|  - Systemd Daemon (smartfetch-browser-daemon) on port 8443                     |
|  - Ephemeral Docker containers (network_mode: none, non-root UID 10001)        |
|  - Native Chromium sandboxing (zero --no-sandbox)                             |
|  - Unix IPC Policy Broker enforcing SSRF blocks against private IPs & metadata|
+-------------------------------------------------------------------------------+
```

The service is fully operational, verified live on-chain, and signed off. The items below represent **non-blocking future enhancements** to be prioritized and developed in subsequent releases without disrupting the stable v1.12.0 baseline.

---

## 2. Scaling & Multi-Worker Capacity

As external adoption and agent query volume grow, scaling browser execution capacity horizontally will be a key focus:

### 2.1 Multi-Worker VPS Fleet & Load Balancing
- **Worker Registry & Health Balancing**: Implement a dynamic worker registry on the Railway gateway that distributes rendering tasks across a pool of isolated VPS daemon nodes.
- **Routing Strategies**: Use health-aware least-connections routing with automatic failover if a worker reports high load or fails health checks.
- **Geographic Distribution**: Deploy VPS worker nodes in multiple regions (e.g., North America, Europe, Asia-Pacific) to minimize latency to target websites.

### 2.2 Container Pre-Warming & Pool Recycling
- **Pre-Warmed Ephemeral Containers**: Maintain a small pool (e.g., 2–4) of pre-launched, idle Docker containers on each worker host. Pre-warming avoids container launch overhead and can reduce total page rendering latency from ~5.5s to <2s.
- **Zero-State Hygiene**: Guarantee that every container is destroyed immediately after handling a single page execution, preventing cross-tenant residue or memory leaks.

### 2.3 Autoscaling & Capacity Management
- **Demand-Based Node Provisioning**: Automate VPS provisioning via cloud provider APIs (DigitalOcean / AWS) triggered by sustained queue depth or concurrency limits.
- **Graceful Draining**: Support node draining where a terminating worker finishes in-flight requests before shutting down.

---

## 3. Observability, Monitoring & Alerting

Enhance system visibility across both tiers without adding latency to the critical execution path:

### 3.1 Distributed Tracing & OpenTelemetry
- **End-to-End Traces**: Propagate trace contexts (`traceparent` headers) across the boundary from Railway MCP requests through the mTLS VPS connection into the Unix IPC policy broker.
- **Timing Breakdowns**: Provide fine-grained latency spans for payment verification, browser initialization, DNS resolution, network transit, DOM rendering, and CDP settlement.

### 3.2 Metrics & Telemetry
- **Prometheus Exporters**: Expose Prometheus-compatible metrics on internal endpoints (worker container churn rate, memory/CPU utilization per render, policy broker blocked requests/SSRF attempts).
- **Payment Metrics**: Track facilitator settlement success rate, settlement latency, and CDP API quota consumption.

### 3.3 Production Alerting
- **mTLS Certificate Expiration Monitoring**: Proactive alerting 30, 14, and 7 days prior to mTLS certificate expiration.
- **Facilitator Health Alerts**: Immediate alerts if the Coinbase CDP facilitator returns HTTP 5xx or exceeds latency thresholds.
- **Host Security Alerts**: Real-time alerts on repeated HMAC replay rejections, invalid client certificates, or abnormal container restart frequencies.

---

## 4. Reliability & Operational Hardening

Operational improvements to maximize uptime and resilience under edge conditions:

### 4.1 Automated Certificate Lifecycle Management
- **Automated Rotation**: Implement automated certificate renewal and rotation scripts using an internal CA or Vault to eliminate manual certificate rotation before the 1-year expiration.
- **Zero-Downtime Reload**: Ensure both Railway and the VPS daemon can reload certificates dynamically on SIGHUP without restarting active connections.

### 4.2 Settlement Dead-Letter Queue (DLQ) & Resilience
- **Asynchronous Settlement Buffer**: If the CDP facilitator experiences transient downtime after a page has been successfully rendered and delivered to the user, buffer the signed settlement payload in an encrypted queue (e.g., Redis or database) for exponential backoff retries.
- **Facilitator Failover**: Support secondary facilitator fallbacks if the primary Coinbase CDP platform endpoint is unreachable.

### 4.3 Policy Broker Rule Customization
- **Configurable Domain Whitelists/Blacklists**: Allow operator-defined blocklists for malware or phishing domains at the broker level.
- **Bandwidth Quotas per Request**: Impose hard per-page total byte limits on outbound HTTP traffic during browser execution to prevent bandwidth abuse from massive streaming media assets.

### 4.4 Scheduled Worker Daemon Image Rebuild & Version Alignment
- **Scheduled Image Rebuild**: Rebuild the `smartfetch-browser-daemon:prod` container image during the first planned maintenance window to synchronize the static version reporting string (`1.11.1` → `1.13.0`), as documented in [`docs/vps-version-consistency-audit.md`](vps-version-consistency-audit.md).
- **Zero-Downtime Deployment**: Execute the container update alongside the scheduled automated mTLS certificate rotation scripts without interrupting the active Railway gateway.

---

## 5. Customer Onboarding & Developer Experience

Lower friction for AI agent developers, enterprises, and individual users adopting SmartFetch:

### 5.1 Interactive Web Playground & Signing Sandbox
- **Hosted Browser Test Suite**: Provide a clean web playground where prospective users can connect MetaMask, Coinbase Wallet, or Rabby and run sample queries (`/fetch` and MCP tools) with real-time EIP-712 signing previews.
- **Receipt & Output Viewer**: Render the extracted Markdown, clean text, links, and transaction confirmation in an interactive UI.

### 5.2 Official Client SDKs
- **Python SDK (`smartfetch-python`)**: Async client with automatic x402 header negotiation, EIP-712 signing (via web3.py / eth-account), and automatic retry on 402 challenges.
- **TypeScript/Node SDK (`@smartfetch/client`)**: Lightweight client compatible with Node.js, Bun, Deno, and browser runtimes with viem/ethers integration.
- **LangChain & AutoGen Plugins**: Drop-in tool integrations for major AI agent orchestration frameworks.

### 5.3 Customer Documentation & Starter Kits
- **Claude Desktop MCP Guide**: Clear walkthrough for adding SmartFetch to `claude_desktop_config.json` with funded buyer wallets.
- **Cursor & Copilot Integration**: Recipe for using SmartFetch MCP as a live documentation scraper within IDE agent workflows.
- **Self-Hosted Deployment Blueprint**: Containerized blueprint for customers wishing to run their own private SmartFetch gateway and isolated VPS cluster.

---

## 6. Prioritization & Roadmap Horizons

```
+-----------------------------------------------------------------------------------+
| HORIZON 1: Near-Term (v1.13.0)                                                    |
| - Distributed OpenTelemetry tracing across Railway gateway and VPS daemon         |
| - Automated mTLS certificate rotation scripts and monitoring alerts               |
| - Rebuild VPS daemon image to synchronize reported version string (v1.13.0)       |
| - Client SDK starter templates (Python / TypeScript)                              |
| - Interactive buyer playground for browser wallet onboarding                      |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| HORIZON 2: Mid-Term (v1.14.0)                                                     |
| - Multi-worker VPS fleet with dynamic load balancing                              |
| - Container pre-warming pool to reduce rendering latency to <2.0s                 |
| - Encrypted settlement Dead-Letter Queue (DLQ) for facilitator retry buffering    |
| - Prometheus metrics exporter and Grafana dashboard                               |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| HORIZON 3: Long-Term (v2.0.0)                                                     |
| - Multi-region global edge workers (US, EU, APAC)                                 |
| - On-demand worker autoscaling via cloud APIs                                     |
| - Multi-facilitator settlement failover (secondary CDP / self-hosted relayer)     |
| - Enterprise team billing, API keys, and multi-wallet balance abstractions        |
+-----------------------------------------------------------------------------------+
```

---

## 7. Change Management Principles

To maintain the rock-solid stability established in v1.12.0:
1. **Zero Downtime**: Production Railway gateway and VPS daemon must remain continuously operational during feature development.
2. **Strict Isolation Boundary**: No future feature may bypass the VPS mTLS + HMAC boundary, eliminate container isolation, or pass `--no-sandbox` to Chromium.
3. **Fail-Closed Financial Settlement**: Payment settlement must remain strictly coupled to verified successful page retrieval.
4. **Contract Stability**: Changes to MCP tool schemas or x402 discovery manifests must maintain backwards compatibility.
