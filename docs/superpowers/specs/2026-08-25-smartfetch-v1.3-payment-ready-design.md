# SmartFetch V1.3 Payment-Ready Infrastructure Design

## Goal

Upgrade SmartFetch V1.2 to a FastAPI/Uvicorn API with fail-closed, Base Sepolia-only x402 protection for `POST /fetch`, while preserving the retrieval engine and the free-mode API behavior.

## Scope and invariants

- Keep `smartfetch/core.py`, `smartfetch/extract.py`, `smartfetch/browser_fetch.py`, `smartfetch/security.py`, and `tests/local_20.py` byte-for-byte unchanged.
- Preserve `GET /health`, `GET /`, `GET /meta`, and `POST /fetch` payloads, status codes, request IDs, response security headers, rate limiting, concurrency limits, and total request timeout as closely as FastAPI permits.
- Keep FastAPI's generated documentation and OpenAPI routes disabled so V1.3 does not unintentionally add public endpoints.
- Keep Railway deployment bound to `HOST` and `$PORT` through `python -m smartfetch.server`.
- Do not require, read, accept, log, or store a seller private key.
- Do not enable Base mainnet or any other mainnet in V1.3.

## Dependencies

Pin the infrastructure packages exactly in `requirements.txt`:

- `fastapi==0.141.1`
- `uvicorn==0.52.4`
- `x402[fastapi,evm]==2.20.0`

The existing retrieval dependencies retain their current compatible-version ranges. Exact pins prevent future FastAPI, Uvicorn, or x402 releases from silently changing the API or payment middleware behavior.

## API boundary

`smartfetch/server.py` will expose a module-level FastAPI `app` and a `main()` function that calls `uvicorn.run(app, host=HOST, port=PORT)`. The retrieval function remains synchronous and unchanged; `/fetch` will submit it to the existing bounded `ThreadPoolExecutor`, await it with the existing total timeout, and retain the nonblocking capacity semaphore.

The FastAPI boundary will manually parse and validate request bodies instead of relying on automatic Pydantic request validation. This preserves the V1.2 `400` response shapes for invalid body size, invalid JSON, non-object JSON, missing URLs, blocked targets, and invalid `max_chars` values. Custom not-found handling will preserve the existing JSON error shape rather than FastAPI's default error document.

Every application response will retain `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and `X-Request-ID`. Each response body will include `request_id`. Successful fetches will include `service_version: "1.3.0"`.

## Payment configuration and fail-closed behavior

Payment setup will live in `smartfetch/payments.py` so the API boundary does not contain wallet or protocol details.

Configuration:

- `X402_ENABLED`: unset or a recognized false value keeps `/fetch` free. Recognized true values enable payment. Any other nonempty value is invalid.
- `X402_PAY_TO`: required only when payment is enabled and must be a 20-byte EVM receiving address encoded as `0x` plus 40 hexadecimal characters.
- `X402_PRICE`: defaults to `$0.005` and must be a positive fixed-dollar price string accepted by the x402 SDK.
- `X402_NETWORK`: defaults to `eip155:84532`, the CAIP-2 identifier for Base Sepolia. V1.3 rejects every other value, including Base mainnet `eip155:8453`.

When `X402_ENABLED` is false or unset, payment middleware is not created or registered. SmartFetch starts normally even if optional payment variables are absent, and `/fetch` remains free for local and remote testing.

When `X402_ENABLED` is true, startup performs all validation and constructs the official async x402 resource server, Base Sepolia exact-EVM scheme, route configuration, and FastAPI middleware. The only protected route key is `POST /fetch`. `GET /health`, `GET /`, and `GET /meta` remain outside the payment route map.

The V1.3 testnet facilitator is `https://x402.org/facilitator`. Initialization or configuration errors propagate and abort startup. SmartFetch must never catch those errors and continue without middleware. No seller signer or private key is part of the resource-server configuration; only the public receiving address is passed as `pay_to`.

In free mode, `/meta` retains the V1.2 payment value `not-enabled-yet`. In enabled mode it reports `x402-enabled-testnet` without disclosing or echoing the receiving address.

## Transient upstream retry

`smartfetch/http_fetch.py` will add a focused helper around the existing `requests.Session.get` call. It retries exactly once for:

- `requests.ConnectionError`
- `requests.Timeout`
- HTTP `502`, `503`, or `504`

The first transient response is closed before retrying. A second transient status follows the existing HTTP error path, and a second network exception propagates to the unchanged browser-fallback behavior in `core.py`. Redirect limits, content-type validation, response-size limits, SSRF validation, and browser fallback remain unchanged.

## Testing strategy

Implementation follows red-green TDD with new focused tests before production changes:

- FastAPI compatibility tests cover free `/health`, `/meta`, `/fetch`, headers, error shapes, version `1.3.0`, and `$PORT` startup behavior.
- Payment configuration tests cover disabled/unset free mode; missing or malformed `X402_PAY_TO`; invalid price; invalid enable flag; Base Sepolia default; rejection of Base mainnet and every non-Base-Sepolia network; middleware initialization failure; and route scoping to only `POST /fetch`.
- Payment middleware smoke testing verifies an unpaid request receives `402` when enabled while `/health` and `/meta` remain free. It uses only a public dummy receiving address and no private key.
- Retry tests cover one retry then success for `502`, `503`, `504`, connection errors, and timeouts; no retry for non-transient status; and failure after exactly two transient attempts.
- Existing `tests/security_smoke.py`, `tests/api_local_smoke.py`, `tests/local_20.py`, `tests/live_20.py`, and `tests/remote_20.py` remain functionally intact. Only the API smoke test's expected service version and startup compatibility may change.

Before commit, run all locally executable existing tests plus the new payment/config/retry tests. `remote_20.py` requires a deployed base URL and `live_20.py` requires public network access; both remain unchanged and will be run when their required environment is available. The required local commit gate is security smoke, API smoke, the unchanged 20-case local suite, and all new V1.3 tests.

## Documentation and deployment

Update `README.md` for V1.3, FastAPI/Uvicorn startup, one-retry behavior, pinned infrastructure dependencies, free-mode testing, Base Sepolia-only payment configuration, fail-closed startup, and the explicit prohibition on seller private keys. Update the Docker command only if needed; `python -m smartfetch.server` remains valid and reads Railway's `$PORT`.

The final implementation is committed to `main`, pushed to `origin/main`, and verified by comparing the local and remote commit IDs.
