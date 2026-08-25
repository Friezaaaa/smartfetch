# SmartFetch V1.3 — payment-ready API

SmartFetch takes a public web URL and returns clean agent-ready text, Markdown, links, metadata, and the retrieval method used. It tries cheap HTTP retrieval first and falls back to a real Chromium browser when needed.

## What changed from V1.2

- FastAPI + Uvicorn now provide the API boundary while preserving the V1.2 response contract.
- Upstream connection/timeout failures and HTTP 502, 503, or 504 responses receive one retry.
- Official x402 middleware can protect only `POST /fetch`; it is disabled by default.
- V1.3 payment configuration is restricted to Base Sepolia testnet and the fixed, configurable `$0.005` default price.
- FastAPI, Uvicorn, and x402 are exactly pinned so infrastructure upgrades are explicit.

The retrieval engine, extraction/browser behavior, SSRF protections, request limits, rate limiting, and concurrency controls are unchanged.

## API

### GET /health

Returns service health/version.

### GET /meta

Machine-readable service description.

### POST /fetch

```json
{
  "url": "https://example.com/article",
  "max_chars": 20000,
  "force_browser": false
}
```

`max_chars` is optional (minimum 1,000; default 20,000; maximum 50,000).

Example response fields:

```json
{
  "success": true,
  "requested_url": "https://example.com/article",
  "final_url": "https://example.com/article",
  "status_code": 200,
  "render_method": "http",
  "title": "…",
  "content": "…",
  "markdown": "…",
  "links": [],
  "word_count": 1000,
  "content_hash": "…",
  "truncated": false,
  "elapsed_ms": 350,
  "request_id": "…",
  "service_version": "1.3.0"
}
```

## Run locally

```bash
python -m pip install -r requirements.txt
python -m smartfetch.server
```

Then:

```bash
curl http://127.0.0.1:8787/health
curl -X POST http://127.0.0.1:8787/fetch \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

Windows users can run `setup_windows.bat`, then `start_windows.bat`.

With `X402_ENABLED` unset or false, startup requires no payment settings and `/fetch` remains free for local and remote testing.

## Base Sepolia x402 test mode

V1.3 supports Base Sepolia only. Configure the public receiving address and enable payment explicitly:

```text
X402_ENABLED=true
X402_PAY_TO=0xYOUR_PUBLIC_RECEIVING_ADDRESS
X402_PRICE=$0.005
X402_NETWORK=eip155:84532
```

`X402_PRICE` and `X402_NETWORK` use the values shown when omitted. When payment is enabled, SmartFetch validates the entire configuration and initializes the official x402 middleware and testnet facilitator before serving. Any invalid address, price, network, unsupported facilitator response, or initialization failure aborts startup; `/fetch` never silently falls back to free access.

Only `POST /fetch` is protected. `/health`, `/`, and `/meta` always remain free. Base mainnet (`eip155:8453`) and every other network are rejected in V1.3.

SmartFetch needs only the public receiving address. Never provide a seller private key to this service: SmartFetch does not require, read, accept, log, or store one.

## Local validation

```bash
python tests/security_smoke.py
python tests/api_local_smoke.py
```

`api_local_smoke.py` intentionally enables private-network access only inside the test process so it can use a localhost fixture.

## Container

```bash
docker build -t smartfetch:v1.3 .
docker run --rm -p 8787:8787 smartfetch:v1.3
```

The container installs Chromium automatically.

## Public deployment

This package is Docker-ready. Railway can deploy the included `Dockerfile` and `railway.json`; any Docker host that provides outbound HTTPS and enough RAM for one Chromium process should also work.

Recommended initial environment variables:

```text
MAX_CONCURRENT_BROWSERS=1
MAX_CONCURRENT_FETCHES=4
DEFAULT_MAX_OUTPUT_CHARS=20000
MAX_OUTPUT_CHARS=50000
FETCH_TIMEOUT_SECONDS=12
BROWSER_TIMEOUT_SECONDS=15
TOTAL_REQUEST_TIMEOUT_SECONDS=25
RATE_LIMIT_PER_MINUTE=30
RATE_LIMIT_BURST=10
X402_ENABLED=false
```

Do **not** set `ALLOW_PRIVATE_NETWORK=1` in production.

### Important SSRF deployment note

Application-level URL validation is included, but browser rendering can execute page subresources. For a paid public deployment, the browser container should additionally be isolated by host/network egress policy from cloud metadata and private RFC1918 networks. We will harden this again before removing the temporary rate limit and before x402 launch.

## Test the public deployment

On Windows run `test_remote_windows.bat`, paste the public URL, and upload `tests/remote_20_results.json`.

Or:

```bash
python tests/remote_20.py https://YOUR-PUBLIC-URL
```

Our deployment gate remains **18/20 minimum**, including all five forced-browser requests.

## Payment launch status

V1.3 is testnet infrastructure only. Do not configure real or mainnet payments yet. Validate the Railway deployment and Base Sepolia payment flow before any future mainnet milestone.
