# SmartFetch V1.3 Payment-Ready Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver SmartFetch V1.3 on FastAPI/Uvicorn with one transient HTTP retry and fail-closed, Base Sepolia-only x402 protection that is disabled by default.

**Architecture:** Keep the synchronous retrieval engine intact behind a compatibility-focused FastAPI boundary. Isolate x402 parsing and official middleware wiring in `smartfetch/payments.py`; eagerly initialize `x402HTTPResourceServer` before serving, then register the official FastAPI middleware with lazy synchronization disabled. Add a narrow retry helper only around the existing upstream HTTP request.

**Tech Stack:** Python 3.12, FastAPI 0.141.1, Uvicorn 0.52.4, x402 2.20.0, requests, unittest, FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-08-25-smartfetch-v1.3-payment-ready-design.md`

## Global Constraints

- Keep `smartfetch/core.py`, `smartfetch/extract.py`, `smartfetch/browser_fetch.py`, `smartfetch/security.py`, and `tests/local_20.py` byte-for-byte unchanged.
- Protect only `POST /fetch`; `GET /health`, `GET /`, and `GET /meta` always remain free.
- `X402_ENABLED=true` must fail startup for missing/invalid configuration, import failures, facilitator failures, unsupported routes, or middleware construction failures.
- Disabled or unset x402 must leave `/fetch` free and must not validate optional payment variables.
- Accept only Base Sepolia `eip155:84532`; reject Base mainnet and every other network.
- Do not require, read, accept, log, or store any seller private key.
- Keep the default fixed price `$0.005` and do not add another price model.
- Preserve Railway `HOST` and `$PORT` behavior.
- Make no implementation commit until the complete local test gate passes.

---

### Task 1: Pin dependencies and add failing payment/config tests

**Files:**
- Modify: `requirements.txt`
- Create: `tests/test_v13_payments.py`
- Create: `smartfetch/payments.py`

**Interfaces:**
- Produces: `X402Settings(enabled: bool, pay_to: str | None, price: str, network: str)`.
- Produces: `load_x402_settings(env: Mapping[str, str] | None = None) -> X402Settings`.
- Produces: `install_x402(app: FastAPI, settings: X402Settings) -> bool`.

- [ ] **Step 1: Pin infrastructure dependencies exactly**

Add these lines without changing existing retrieval dependency ranges:

```text
fastapi==0.141.1
uvicorn==0.52.4
x402[fastapi,evm]==2.20.0
```

- [ ] **Step 2: Write payment-setting tests before implementation**

Create unittest cases that derive literal expectations and cover:

```python
load_x402_settings({}) == X402Settings(False, None, "$0.005", "eip155:84532")
load_x402_settings({"X402_ENABLED": "false", "X402_NETWORK": "eip155:8453"}).enabled is False
load_x402_settings({"X402_ENABLED": "true", "X402_PAY_TO": VALID_ADDRESS}).price == "$0.005"
```

Assert `ValueError` for invalid enable values, missing/short/zero receiving addresses, zero/malformed prices, Base mainnet, and non-Base-Sepolia networks.

- [ ] **Step 3: Run the payment tests and verify RED**

Run:

```powershell
python -m unittest tests.test_v13_payments -v
```

Expected: failure because `smartfetch.payments` does not exist.

- [ ] **Step 4: Implement strict settings parsing**

Use recognized true values `1,true,yes,on` and false values empty/`0,false,no,off`, case-insensitively. Validate enabled configurations only. Validate the public receiving address as nonzero `0x` plus 40 hexadecimal characters and price as a positive fixed-dollar string with at most six decimal places.

- [ ] **Step 5: Verify settings GREEN**

Run the payment test module and require all settings cases to pass.

- [ ] **Step 6: Add failing middleware behavior tests**

Using a real FastAPI app and official x402 classes, patch only `HTTPFacilitatorClient.get_supported` to return:

```python
SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme="exact",
    network="eip155:84532",
)])
```

Test that an unpaid `POST /fetch` receives `402`, while `/health` and `/meta` reach their free handlers. Patch facilitator support failure and assert `install_x402` raises `RuntimeError` before the app can serve. Inspect the app's observable responses rather than asserting mock call counts.

- [ ] **Step 7: Run middleware tests and verify RED**

Expected: failure because `install_x402` is not implemented.

- [ ] **Step 8: Implement official x402 wiring with eager fail-closed initialization**

Inside `install_x402`, import the official pinned components only when enabled, create `HTTPFacilitatorClient(FacilitatorConfig(url="https://x402.org/facilitator"))`, register `ExactEvmServerScheme` on `eip155:84532`, and build only this route:

```python
{"POST /fetch": RouteConfig(
    accepts=[PaymentOption(
        scheme="exact",
        pay_to=settings.pay_to,
        price=settings.price,
        network=settings.network,
    )],
    mime_type="application/json",
    description="SmartFetch public-web retrieval",
)}
```

Call `x402HTTPResourceServer(server, routes).initialize()` before middleware registration to fetch facilitator support and validate routes. Register the official `payment_middleware(..., sync_facilitator_on_start=False)` on the FastAPI app. Wrap any enabled initialization exception in `RuntimeError` and propagate it. Return `False` without importing or constructing x402 when disabled.

- [ ] **Step 9: Verify payment/config GREEN**

Run all payment tests and require free-route and unpaid-402 behavior to pass.

### Task 2: Add one transient upstream retry

**Files:**
- Create: `tests/test_v13_retry.py`
- Modify: `smartfetch/http_fetch.py`

**Interfaces:**
- Produces: `_get_with_retry(url: str) -> requests.Response` used only by `http_fetch`.

- [ ] **Step 1: Write retry behavior tests first**

Patch the external `SESSION.get` boundary with full response fixtures. Verify exactly two attempts for first-attempt `502`, `503`, `504`, `requests.ConnectionError`, and `requests.Timeout`, followed by success. Verify one attempt for `500` and `404`. Verify two attempts and the existing error/exception when both attempts are transient. Verify the first transient response is closed.

- [ ] **Step 2: Run retry tests and verify RED**

Expected: assertion failure because current `http_fetch` calls `SESSION.get` once.

- [ ] **Step 3: Implement the minimal retry helper**

Loop for two attempts. Catch only `requests.ConnectionError` and `requests.Timeout`; re-raise after the second. When status is in `{502, 503, 504}`, close and retry only on the first attempt. Return all other responses unchanged so the existing status handling remains authoritative.

- [ ] **Step 4: Verify retry GREEN**

Run the retry module and require all cases to pass.

### Task 3: Replace the API wrapper with FastAPI/Uvicorn

**Files:**
- Create: `tests/test_v13_api.py`
- Modify: `smartfetch/server.py`
- Modify: `smartfetch/config.py`
- Modify: `tests/api_local_smoke.py`

**Interfaces:**
- Produces: `create_app() -> FastAPI`.
- Produces: module-level `app` for ASGI servers.
- Preserves: `main()` invoked by `python -m smartfetch.server` and binding to `HOST`/`PORT`.

- [ ] **Step 1: Write FastAPI compatibility tests first**

Use `TestClient(create_app())` with x402 disabled. Assert literal V1.2-compatible outcomes:

- `/health` returns `200`, `ok: true`, `service: SmartFetch`, `version: 1.3.0`, and a request ID.
- `/` and `/meta` return the existing service description/input/endpoint shape and `payment: not-enabled-yet`.
- unknown routes and unsupported methods return `404` with `error_code: not_found`.
- empty/oversized bodies, malformed JSON, non-object JSON, and missing URLs retain their existing `400` shapes.
- every application response has `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and matching body/header request IDs.
- `/docs`, `/redoc`, and `/openapi.json` remain unavailable.

Patch only the retrieval boundary `smartfetch.server.smart_fetch` for route-contract unit cases; the existing API smoke test exercises the real retrieval path.

- [ ] **Step 2: Run API tests and verify RED**

Expected: failure because the existing module has no FastAPI `app` or `create_app`.

- [ ] **Step 3: Implement the minimal compatibility-focused FastAPI boundary**

Retain the existing rate bucket, semaphore, thread pool, input validation, timeout/error mapping, and success augmentation. Parse raw bodies manually. Use `asyncio.get_running_loop().run_in_executor` plus `asyncio.wait_for` so Uvicorn's event loop is not blocked. Add the request/security-header middleware outside x402 so even `402` responses carry a request ID and security headers. Disable docs/OpenAPI URLs.

- [ ] **Step 4: Configure x402 last and fail during module startup**

`create_app()` loads settings, registers normal routes and middleware, then calls `install_x402`. Module-level `app = create_app()` makes invalid enabled configuration or facilitator failure abort import before Uvicorn serves. Disabled mode stays free.

- [ ] **Step 5: Preserve Railway startup**

Implement:

```python
def main():
    uvicorn.run(app, host=HOST, port=PORT)
```

- [ ] **Step 6: Update the API smoke test only where necessary**

Set `X402_ENABLED=false` in the subprocess environment, allow the Uvicorn startup window, and change only the service-version assertion from `1.2.0` to `1.3.0`. Add `/meta` free-mode verification without changing the real fetch fixture.

- [ ] **Step 7: Verify API GREEN**

Run the new API tests and the existing API smoke test.

### Task 4: Document V1.3 and verify the complete scope

**Files:**
- Modify: `README.md`
- Verify: `Dockerfile`, `railway.json`, `start_windows.bat`
- Verify unchanged: `smartfetch/core.py`, `smartfetch/extract.py`, `smartfetch/browser_fetch.py`, `smartfetch/security.py`, `tests/local_20.py`, `tests/live_20.py`, `tests/remote_20.py`

**Interfaces:**
- Documents: free mode, Base Sepolia-only paid mode, fail-closed startup, public receiving address, no private key, default `$0.005`, retry behavior, and Railway `$PORT`.

- [ ] **Step 1: Update README for V1.3**

Document exact variables and defaults:

```text
X402_ENABLED=false
X402_PAY_TO=0xYOUR_PUBLIC_RECEIVING_ADDRESS
X402_PRICE=$0.005
X402_NETWORK=eip155:84532
```

State that enabled startup contacts the official testnet facilitator and aborts on any invalid configuration or initialization error; Base mainnet is rejected; seller private keys must never be supplied to SmartFetch.

- [ ] **Step 2: Confirm Docker/Railway startup needs no behavioral change**

Verify `CMD ["python", "-m", "smartfetch.server"]`, `HOST=0.0.0.0`, and `PORT` environment handling remain valid. Change these files only if a failing deployment check proves it necessary.

- [ ] **Step 3: Run the full local test gate fresh**

Run in this order with `X402_ENABLED=false` unless a payment test explicitly enables it:

```powershell
python -m unittest discover -s tests -p 'test_v13_*.py' -v
python tests/security_smoke.py
python tests/api_local_smoke.py
python tests/local_20.py
```

Run `tests/live_20.py` if public network access is available. Do not run `tests/remote_20.py` without its required deployed base URL; verify it remains byte-identical instead.

- [ ] **Step 4: Verify protected-file hashes and scope**

Require these Git blob hashes to remain unchanged:

```text
1ac09beef1d37e53744131c25bff7fe4c1d6a6d2  smartfetch/core.py
69aeaa57d88e94bc6a6fa71dd35a8f34431c97d0  smartfetch/extract.py
233cf4c31cfc6c7fb54c403d680c79cb6bc3618d  smartfetch/browser_fetch.py
f04e531327191d82f917648e14e56c9936c1ff10  smartfetch/security.py
6d0f65abe1bb40eb2dd1f63d31c5ad9dae2ab751  tests/local_20.py
```

Also require `tests/live_20.py` and `tests/remote_20.py` to be unchanged from `HEAD`, run `git diff --check`, and audit `smartfetch/` for any private-key environment access.

- [ ] **Step 5: Commit only after the gate passes**

Stage the approved implementation, tests, README, and plan. Review the staged diff, then commit with:

```text
feat: add V1.3 payment-ready infrastructure
```

- [ ] **Step 6: Push and verify GitHub**

Push `main` to `origin`, query `refs/heads/main`, and require the remote commit ID to match local `HEAD`.
