# SmartFetch V1.10.4 — privacy-safe retrieval diagnostics

SmartFetch takes a public web URL and returns clean agent-ready text, Markdown, links, metadata, and the retrieval method used. It tries cheap HTTP retrieval first and falls back to a real Chromium browser when needed.

SmartFetch V1.10.3 is live in production. This branch prepares V1.10.4 for
review; it does not itself deploy the service.

## V1.10.4 retrieval diagnostics

- Failed HTTP retrievals now carry finite, typed diagnostic codes through the
  existing browser fallback and final HTTP 502 path without changing retrieval
  strategy, retries, timeouts, fallback, or client response behavior.
- The existing final `tool_failed` activity event can include only a normalized
  target hostname, strategy, phase, finite failure code, attempt flags, and an
  optional bounded upstream HTTP status. IP literals are recorded only as the
  categorical value `ip-literal`.
- URL credentials, ports, paths, queries, fragments, exception messages,
  response content, and payment material never enter these diagnostic fields.

## V1.10.3 access-log privacy patch

- Uvicorn application access logs retain the request method, route path, HTTP
  version, and response status while omitting the entire query string.
- Query parameters remain available to routing and request handlers; the
  change is confined to access-log formatting and does not alter MCP, x402,
  payment, retrieval, or discovery behavior.
- SmartFetch's structured activity events remain allowlisted and continue to
  provide request IDs, client categories, statuses, and durations where those
  fields are currently available.
- Hosting-provider infrastructure logs are outside the application logging
  boundary and may require separate provider controls if they record full
  request targets independently.

## V1.10.2 AgentCash discovery

- `/openapi.json` now includes the canonical AgentCash `info.x-guidance` and
  operation-level `x-payment-info` fields for paid `POST /fetch` discovery.
- AgentCash pricing metadata is derived from SmartFetch's configured x402
  price. The live x402 challenge remains authoritative for the exact scheme,
  network, asset contract, atomic amount, public payee, and settlement.
- V1.10.2 does not change retrieval, rendering, security, payment verification,
  settlement, facilitator selection, or any MCP tool contract.

## V1.10.1 compatibility patch

- Activity events are emitted as one compact JSON line through a dedicated
  plain stdout handler, with explicit `message` and `level` fields.
- The community x402 manifest identifies the paid HTTP resource and structured
  MCP endpoint without an ambiguous bare tool-name array. The four tools remain
  discoverable through MCP `tools/list` and `/meta`.
- `/openapi.json` describes the active x402 v2 exact network, USDC on Base mainnet
  asset, configured price, and atomic amount for `POST /fetch`.

## What changed in V1.10

- V1.10 adds privacy-safe structured activity events for MCP discovery, tool
  attempts, x402 challenges, verified payments, execution, and settlement.
- `GET /.well-known/x402` provides a free, proxy-aware community discovery
  manifest for buyers such as Agent402. It is a community convention rather
  than a finalized x402 Foundation protocol endpoint.
- Runnable Python and TypeScript MCP buyers list tools for free, enforce a
  `$0.005` maximum payment, handle challenge → pay → retry, and print the
  result plus settlement receipt.
- The production MCP server exposes four live paid tools backed by the same
  retrieval engine: `fetch_webpage`, `webpage_to_markdown`,
  `extract_webpage_text`, and `render_webpage`.
- Every tool has its own official x402 MCP Bazaar declaration and unique
  `mcp://tool/<tool-name>` payment resource while sharing the existing exact
  `$0.005` payment requirements.
- HTTP `POST /fetch` and MCP tool execution are live with x402 `exact`
  payments on Base mainnet at `$0.005` per execution.
- Free `/.well-known/x402`, `/docs`, `/openapi.json`, `/llms.txt`,
  `/robots.txt`, `/sitemap.xml`, and `/meta` routes serve humans, crawlers,
  and agents.
- Runtime discovery links use FastAPI/Starlette's proxy-aware request scheme
  and host. The Railway hostname is not embedded in runtime discovery output.
- SmartFetch V1.10.3 is the currently published Official MCP Registry release;
  V1.10.4 requires a separate publication after release.

All V1.8 HTTP payment, Bazaar, Registry, and native MCP behavior remains
unchanged:

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
  "service_version": "1.10.4"
}
```

When x402 payment protection is enabled, the `PAYMENT-REQUIRED` header for
`POST /fetch` includes a Bazaar declaration with the `url`, `max_chars`, and
`force_browser` input contract plus a representative successful response.
`/`, `/health`, `/meta`, `/.well-known/x402`, `/docs`, `/openapi.json`,
`/llms.txt`, `/robots.txt`, and `/sitemap.xml` remain free and do not advertise
paid Bazaar metadata.

## Free discovery routes

- `/.well-known/x402` is a community buyer manifest with the paid HTTP
  resource, structured MCP endpoint, configured price/network, and dynamic
  machine-readable links.
- `/docs` is a concise human- and crawler-readable service guide.
- `/openapi.json` is the explicit OpenAPI 3.1 contract for `POST /fetch`.
- `/llms.txt` summarizes endpoints, tools, price, and source for agents.
- `/robots.txt` allows public crawling and points to `/sitemap.xml`.
- `/sitemap.xml` lists only free discovery content, not `/fetch` or `/mcp`.

All absolute links in these responses and `/meta` are generated from the
framework-resolved public request URL. The existing Uvicorn/Railway trusted
proxy configuration supplies the external HTTPS scheme; application code does
not parse raw forwarded headers.

## AgentCash discovery

AgentCash-compatible agents can inspect SmartFetch without making a paid
request:

```bash
npx agentcash@latest discover https://smartfetch-production-ea53.up.railway.app
npx agentcash@latest check https://smartfetch-production-ea53.up.railway.app/fetch
```

The current official AgentCash CLI also supports this optional free command:

```bash
npx agentcash@latest add https://smartfetch-production-ea53.up.railway.app
```

`add` discovers the origin, installs its generated skill into supported agent
clients, and adds the origin to the AgentCash MCP server description. It is an
optional persistence/install step; it does not invoke SmartFetch `POST /fetch`.

`agentcash fetch` is a paid action. Do not run it without explicit approval and
a buyer-controlled spending policy. A future approved call would use this
placeholder form:

```bash
# PAID: do not run without explicit approval.
npx agentcash@latest fetch \
  https://smartfetch-production-ea53.up.railway.app/fetch \
  -m POST \
  -b '<EXPLICITLY_APPROVED_SMARTFETCH_REQUEST>'
```

Use SmartFetch as a paid fallback when ordinary access to a public HTTP(S)
webpage fails, returns incomplete content, requires JavaScript rendering, or
needs clean text or Markdown. SmartFetch does not bypass CAPTCHAs,
authentication, paywalls, robots restrictions, or access controls, and cannot
guarantee access to every website.

## MCP

Remote MCP clients connect to `/mcp` using Streamable HTTP. The production
server exposes exactly four live tools:

```text
fetch_webpage
  url: required string
  max_chars: optional integer (default 20000, minimum 1000, maximum 50000)
  force_browser: optional boolean (default false)

webpage_to_markdown
  url: required string
  max_chars: optional integer (default 20000, minimum 1000, maximum 50000)
  force_browser: optional boolean (default false)

extract_webpage_text
  url: required string
  max_chars: optional integer (default 20000, minimum 1000, maximum 50000)
  force_browser: optional boolean (default false)

render_webpage
  url: required string
  max_chars: optional integer (default 20000, minimum 1000, maximum 50000)
```

`fetch_webpage` returns the complete existing SmartFetch result.
`webpage_to_markdown` returns Markdown and core retrieval metadata without
duplicating the full text. `extract_webpage_text` returns clean text and core
metadata without Markdown. `render_webpage` always starts with browser
rendering and returns the complete result. Every tool uses the same SSRF
validation, executor, request timeout, concurrency cap, output cap, HTTP
retrieval, and browser behavior as `POST /fetch`; none makes an HTTP request
back to the public API.

When x402 is enabled, an unpaid `tools/call` returns the native MCP x402 payment
challenge. A valid payment is verified before retrieval and settled once after
successful tool execution. The network, public payee, and price come from
`X402_NETWORK`, `X402_PAY_TO`, and `X402_PRICE`. No seller private key or wallet
secret is accepted or required.

Each unpaid MCP payment challenge includes a matching Bazaar declaration with
transport `streamable-http`, an accurate input/output contract, and its unique
resource: `mcp://tool/fetch_webpage`, `mcp://tool/webpage_to_markdown`,
`mcp://tool/extract_webpage_text`, or `mcp://tool/render_webpage`. The existing
HTTP Bazaar resource for `POST /fetch` is separate and unchanged.

The root `server.json` describes the public remote endpoint registered with the
Official MCP Registry. V1.10.3 remains the currently published Registry
version; V1.10.4 requires a separate publication after release.
Registry metadata keeps its required fixed remote URL; runtime discovery routes
derive their URLs from each proxy-aware request. The MCP Bazaar declarations do
not assert indexing by Coinbase Bazaar or downstream MCP directories.

Useful natural discovery phrases include: read a webpage, fetch a webpage,
scrape a URL, retrieve website content, convert webpage to Markdown, extract
clean text from a website, read a JavaScript website, browser render a webpage,
web scraping for an AI agent, get webpage content for an agent, website to
Markdown, and fetch public URL.

## Paying MCP client examples

- [Python client](examples/python/paid_mcp_client.py)
- [TypeScript client](examples/typescript/paid-mcp-client.ts)
- [Setup and funding guide](examples/README.md)

Both clients connect over Streamable HTTP, run free `tools/list`, enforce a
maximum `$0.005` payment, call `fetch_webpage`, and print the settlement
receipt. **Running either client can spend real USDC on Base mainnet.** Use a
separate low-balance CDP-managed wallet and keep `CDP_API_KEY_ID`,
`CDP_API_KEY_SECRET`, and `CDP_WALLET_SECRET` in secret storage.

## Activity events

SmartFetch writes compact JSON events named `mcp_initialized`, `tools_listed`,
`tool_call_attempted`, `payment_challenged`, `payment_verified`,
`tool_started`, `tool_completed`, `tool_failed`, and `payment_settled`.

The event schema is deliberately allowlisted. It can contain only timestamp,
message/level, opaque request ID, transport, tool, route, stage/outcome,
status, duration, payment presence/stage/network/asset/amount, a finite safe
failure reason, and a coarse client category. A final failed retrieval may also
include the normalized target hostname, retrieval strategy/phase, a finite
failure code, attempt flags, and a bounded upstream status. It never includes
complete URLs, URL credentials/ports/paths/queries/fragments, webpage content,
request bodies, headers, payment signatures/payloads, wallet or payee
addresses, IP literals, CDP credentials, transaction hashes, or exception
messages.

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

Among the HTTP API routes, only `POST /fetch` is protected; MCP tool execution
is paid separately at the MCP layer. `/health`, `/`, `/meta`,
`/.well-known/x402`, `/docs`, `/openapi.json`, `/llms.txt`, `/robots.txt`, and
`/sitemap.xml` always remain free. Enabled `/meta` responses report
`x402-enabled-testnet` or
`x402-enabled-mainnet` without exposing configuration values.

SmartFetch uses `X402_PAY_TO` as the public receiving address and does not need `CDP_WALLET_SECRET`. Never provide a seller MetaMask private key or recovery phrase: SmartFetch does not require, read, accept, log, or store either one.

## Local validation

```bash
python tests/security_smoke.py
python tests/api_local_smoke.py
```

`api_local_smoke.py` intentionally enables private-network access only inside the test process so it can use a localhost fixture.

## Container

```bash
docker build -t smartfetch:v1.10.4 .
docker run --rm -p 8787:8787 smartfetch:v1.10.4
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

Application-level URL validation is included, but browser rendering can execute page subresources. For a paid public deployment, the browser container should additionally be isolated by host/network egress policy from cloud metadata and private RFC1918 networks. Production should retain this defense in depth alongside the application controls and rate limits.

## Test the public deployment

On Windows run `test_remote_windows.bat`, paste the public URL, and upload `tests/remote_20_results.json`.

Or:

```bash
python tests/remote_20.py https://YOUR-PUBLIC-URL
```

Our deployment gate remains **18/20 minimum**, including all five forced-browser requests.

## Production release status

SmartFetch V1.10.3 is live in production. The production MCP server still
exposes exactly four tools: `fetch_webpage`, `webpage_to_markdown`,
`extract_webpage_text`, and `render_webpage`. HTTP `POST /fetch` and all four MCP
tools use x402 `exact` payments on Base mainnet at `$0.005` per execution.

Free `/.well-known/x402` discovery, privacy-safe structured activity logging,
and the guarded buyer examples are live V1.10 additions. V1.10.3 is the
currently published Official MCP Registry version; V1.10.4 is not published or
deployed by this repository change. Base Sepolia remains supported for testnet use, and the
active payment network remains controlled by `X402_NETWORK`: `eip155:84532` for
Base Sepolia or `eip155:8453` for Base mainnet.
