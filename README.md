# SmartFetch V1.4 — mainnet-ready payment infrastructure

SmartFetch takes a public web URL and returns clean agent-ready text, Markdown, links, metadata, and the retrieval method used. It tries cheap HTTP retrieval first and falls back to a real Chromium browser when needed.

## What changed in V1.4

- Base Sepolia remains the default x402 network and continues to use `https://x402.org/facilitator` without CDP credentials.
- Base mainnet can use Coinbase's authenticated CDP facilitator when explicitly selected.
- Mainnet startup requires both CDP API credentials and fails closed for invalid credentials or missing facilitator support.
- `/meta` distinguishes enabled testnet and mainnet payment modes.
- `cdp-sdk`, FastAPI, Uvicorn, and x402 are exactly pinned so infrastructure upgrades are explicit.

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
  "service_version": "1.4.0"
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

## x402 payment modes

Payment remains disabled unless explicitly enabled. Base Sepolia is the default and requires only the public receiving address:

```text
X402_ENABLED=true
X402_PAY_TO=0xYOUR_PUBLIC_RECEIVING_ADDRESS
X402_PRICE=$0.005
X402_NETWORK=eip155:84532
```

`X402_PRICE` and `X402_NETWORK` use the values shown when omitted. The default price remains `$0.005`.

Base mainnet must be selected explicitly and uses Coinbase's authenticated CDP facilitator:

```text
X402_ENABLED=true
X402_PAY_TO=0xYOUR_PUBLIC_RECEIVING_ADDRESS
X402_PRICE=$0.005
X402_NETWORK=eip155:8453
CDP_API_KEY_ID=YOUR_CDP_API_KEY_ID
CDP_API_KEY_SECRET=YOUR_CDP_API_KEY_SECRET
```

Mainnet requires both CDP credentials. Missing, invalid, or unusable credentials, unsupported Base-mainnet exact payments, or any facilitator/middleware initialization failure aborts startup. SmartFetch never falls back to free access or the testnet facilitator when mainnet is selected. Every network other than Base Sepolia and Base mainnet is rejected.

Only `POST /fetch` is protected. `/health`, `/`, and `/meta` always remain free. Enabled `/meta` responses report `x402-enabled-testnet` or `x402-enabled-mainnet` without exposing configuration values.

SmartFetch uses `X402_PAY_TO` as the public receiving address and does not need `CDP_WALLET_SECRET`. Never provide a seller MetaMask private key or recovery phrase: SmartFetch does not require, read, accept, log, or store either one.

## Local validation

```bash
python tests/security_smoke.py
python tests/api_local_smoke.py
```

`api_local_smoke.py` intentionally enables private-network access only inside the test process so it can use a localhost fixture.

## Container

```bash
docker build -t smartfetch:v1.4 .
docker run --rm -p 8787:8787 smartfetch:v1.4
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

V1.4 contains mainnet-ready configuration and fail-closed initialization, but this repository change does not deploy it, change Railway variables, or execute a live mainnet payment. Keep production on Base Sepolia until mainnet credentials and an explicit deployment are separately approved.
