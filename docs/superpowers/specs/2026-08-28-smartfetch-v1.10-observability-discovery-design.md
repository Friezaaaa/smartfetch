# SmartFetch V1.10 Observability and Buyer Discovery Design

## Context

SmartFetch V1.9 is live with four paid MCP tools and one paid HTTP retrieval
route. Railway traffic shows that agents can initialize MCP and list tools, but
the current edge logs cannot distinguish discovery, payment challenges,
verified payments, tool execution, or settlement. One crawler also requested
`/.well-known/x402`, received 404, then fell back to OpenAPI and `/fetch`.

V1.10 makes those paths visible and easier to use without adding a fifth tool,
changing retrieval, changing price, or performing a production payment.

## Goals

- Emit structured, privacy-safe activity events for HTTP and MCP discovery,
  tool attempts, payment stages, execution outcomes, and settlement.
- Add a free, proxy-aware `GET /.well-known/x402` service manifest compatible
  with the community discovery shape used by Agent402.
- Add runnable Python and TypeScript examples that list tools for free, enforce
  a `$0.005` per-payment limit, call one SmartFetch tool, and print the result
  and settlement receipt.
- Link the new manifest and examples from `/meta`, `/docs`, `/llms.txt`, and the
  root README.
- Bump service and registry metadata to `1.10.0` while preserving the four
  existing tools and all V1.9 payment behavior.

## Non-goals and invariants

- Do not add web search or any other tool in this release.
- Do not change `smartfetch/core.py`, extraction, rendering, SSRF validation,
  rate limits, concurrency, timeouts, or output limits.
- Do not change the four tool names, order, descriptions, inputs, projections,
  Bazaar resources, or `$0.005` default price.
- Keep MCP initialize and tools/list free. Keep all discovery routes free.
- Do not log target URLs, extracted content, request bodies, headers, payment
  payloads/signatures, wallet addresses, receiving addresses, IP addresses,
  CDP credentials, or exception text that may contain caller data.
- Do not create a wallet, fund a wallet, make a testnet or mainnet payment,
  deploy, publish, or push as part of implementation verification.
- `/.well-known/x402` is documented as a community discovery manifest, not a
  finalized x402 Foundation protocol endpoint.

## Privacy-safe event model

Create `smartfetch/activity.py` as the only module that emits activity logs.
It uses a request-scoped `ContextVar` and writes one compact JSON object to the
`smartfetch.activity` logger. The emitter accepts only these fields:

- `event`
- `timestamp`
- `request_id`
- `transport` (`http` or `mcp`)
- `tool`
- `stage`
- `outcome`
- `status`
- `duration_ms`

Unknown fields are discarded. Values are normalized to bounded strings or a
non-negative integer duration. Logging failures never change API behavior.

Events are:

- `mcp_initialized`
- `tools_listed`
- `tool_call_attempted`
- `payment_challenged`
- `payment_verified`
- `tool_started`
- `tool_completed`
- `tool_failed`
- `payment_settled`

The outer FastAPI middleware reads an MCP request body only to extract the
JSON-RPC `method` and, for `tools/call`, `params.name`. It does not retain or
log arguments. Reading the body must be covered by an integration test proving
the real MCP route still receives and processes it.

For MCP paid tools, use x402 2.20.0's official `PaymentWrapperHooks`:

- `on_before_execution` emits `payment_verified`;
- the underlying handler wrapper emits `tool_started`, `tool_completed`, or
  `tool_failed` with elapsed time;
- `on_after_settlement` emits `payment_settled`;
- an outer result observer classifies payment-required results and emits
  `payment_challenged` without inspecting or logging the payment payload.

For HTTP `POST /fetch`, the existing outer middleware observes only path,
status, request state set by the x402 middleware, and the presence of the
standard settlement-response header. The endpoint emits execution lifecycle
events around `_run_fetch`. No request field is included in an event.

## `/.well-known/x402` manifest

`public_urls()` adds `x402` with the dynamic absolute manifest URL. The new
pure builder receives `urls` and `X402Settings` and returns JSON containing:

- `spec: "agent402-service-manifest/1"`;
- `version: 1`;
- SmartFetch name, summary, homepage, and GitHub repository;
- `resources` containing only the paid absolute `/fetch` URL;
- dynamic links to `/mcp`, `/openapi.json`, `/llms.txt`, `/docs`, and `/meta`;
- the exact four MCP tool names;
- configured payment enabled state, x402 version 2, `exact` scheme, configured
  price, configured CAIP-2 network, and `USDC` asset label.

The manifest omits payee/wallet addresses, credentials, headers, signatures,
and internal Railway addresses. URLs come only from Starlette's proxy-resolved
request URL. The route is registered before the catch-all and is not protected
by x402 middleware.

## Buyer examples

Create:

- `examples/python/paid_mcp_client.py`
- `examples/typescript/paid-mcp-client.ts`
- `examples/README.md`

Both examples use the production Streamable HTTP MCP URL, list tools before
payment, set a per-payment maximum exactly equal to `$0.005`, call
`fetch_webpage`, allow the official wrapper to perform challenge → pay → retry,
and print tool content plus settlement information.

The Python example uses a CDP-managed EVM server account adapted through
`EvmLocalAccount` and `EthAccountSigner`, a standard x402 client with
`max_amount_per_payment: "$0.005"`, the MCP SDK's
`streamable_http_client`, and `x402MCPSession`. The TypeScript example uses
`CdpX402Client`, `StreamableHTTPClientTransport`, and
`wrapMCPClientWithPayment`, with Base-mainnet USDC atomic spend controls capped
at 5,000 units. Credentials come only from documented environment variables.

Examples are syntax/type checked without contacting SmartFetch or CDP. Tests
exercise their configuration helpers or compile/import boundaries rather than
asserting prose or source lines.

## Documentation and versioning

- Set `SERVICE_VERSION` and `server.json` version to `1.10.0`.
- Preserve the official registry server name, repository, description, and
  single Railway remote.
- Update README status and container tags to V1.10.
- Add the manifest URL to `/meta.discovery`, `/docs`, and `/llms.txt`.
- Link all buyer examples from README and `/llms.txt` using GitHub URLs.
- Keep the Glama verification response byte-for-byte equivalent.

## Verification and release observation

Automated verification must prove:

- all existing tests plus V1.10 tests pass;
- tools/list still returns exactly the same four tools;
- every unpaid tool is challenged before `_run_fetch`;
- paid mocked calls log verification, execution, and settlement once;
- no emitted event contains forbidden request/payment/customer data;
- the manifest is free, proxy-aware, mainnet-aware in production settings,
  and contains no secret or payee;
- examples parse/compile without making network calls;
- Glama and Registry identities remain unchanged except the version bump.

After a separately authorized deployment, observe seven days of activity. Add
search only if agents reach tool calls but retrieval use indicates missing URL
discovery. If traffic remains only initialize/list, improve distribution. If
challenges occur without settlements, focus on buyer compatibility and wallet
setup before adding tools.
