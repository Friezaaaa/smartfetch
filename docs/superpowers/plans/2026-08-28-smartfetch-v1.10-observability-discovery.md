# SmartFetch V1.10 Observability and Buyer Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make real agent/payment activity observable, add a free x402 community discovery manifest, and provide guarded paying-client examples without changing the existing four tools or retrieval/payment contracts.

**Architecture:** Add one allowlisted activity emitter with request context, instrument FastAPI and official x402 MCP lifecycle boundaries, add a pure manifest builder beside existing discovery documents, and keep buyer examples isolated under `examples/`. All behavior changes are driven by focused integration tests before production edits.

**Tech Stack:** Python 3.12, FastAPI 0.141.1, Starlette, Uvicorn 0.52.4, MCP 1.29.0, x402 2.20.0, CDP SDK 1.47.1, unittest, FastAPI TestClient, TypeScript MCP/x402 SDKs.

**Spec:** `docs/superpowers/specs/2026-08-28-smartfetch-v1.10-observability-discovery-design.md`

## Global constraints

- Preserve all four V1.9 tool contracts, projections, unique MCP resources,
  HTTP `/fetch`, Bazaar metadata, x402 exact flow, and fail-closed startup.
- Never log URLs, contents, request bodies, headers, signatures, payment
  payloads, wallet/payee addresses, IPs, credentials, or exception messages.
- Keep every discovery route, MCP initialize, and tools/list free.
- Do not change retrieval/security modules or dependency pins.
- Do not deploy, publish, push, create/fund a wallet, or make a payment.
- Follow strict RED → GREEN → REFACTOR for every production behavior.

---

### Task 1: Specify the privacy-safe activity emitter

**Files:**
- Create: `tests/test_v110_activity.py`
- Create: `smartfetch/activity.py`

- [ ] Write tests that name these breaks: an unknown/sensitive field reaches a
  log; caller values can override timestamp/request context; malformed values
  crash a request; an event is not valid single-line JSON.
- [ ] Run `python -m unittest tests.test_v110_activity -v` and verify RED because
  `smartfetch.activity` does not exist.
- [ ] Implement a `ContextVar` request ID, bounded allowlisted normalization,
  UTC timestamp generation, and exception-safe JSON logging.
- [ ] Re-run the focused test and require GREEN.

### Task 2: Specify and implement the free x402 manifest

**Files:**
- Modify: `tests/test_v19_discovery.py`
- Modify: `smartfetch/discovery.py`
- Modify: `smartfetch/server.py`

- [ ] Add literal expectations for `public_urls()['x402']`, the manifest shape,
  configured price/network, exact four tools, and all dynamic links.
- [ ] Add route integration assertions proving `GET /.well-known/x402` returns
  200 with payments enabled and no payment header.
- [ ] Add a forbidden-value test covering active payee, CDP terms, private key,
  payment signature, fixed Railway host, and internal upstream address.
- [ ] Run the V1.10 manifest cases and verify RED (missing URL/builder/route).
- [ ] Implement `x402_manifest(urls, settings)`, wire the free route before the
  catch-all, and add its link to `/meta.discovery`, docs, and llms.txt.
- [ ] Re-run the manifest/discovery slice and require GREEN.

### Task 3: Instrument MCP discovery and paid tool lifecycle

**Files:**
- Modify: `tests/test_v110_activity.py`
- Modify: `smartfetch/mcp_server.py`
- Modify: `smartfetch/server.py`

- [ ] Add real MCP integration tests for initialize, tools/list, and unpaid
  tools/call. Capture `smartfetch.activity` output and assert exact event names,
  transport/tool/status fields, and absence of URL/body/payment data.
- [ ] Verify RED because no events exist.
- [ ] In the outer response middleware, set/reset request context and parse only
  MCP `method` plus `params.name`; emit initialize/list/attempt events after the
  response without retaining arguments.
- [ ] Add an execution wrapper around each underlying tool handler for started,
  completed, failed, and duration events.
- [ ] Pass official `PaymentWrapperHooks` to each paid wrapper for verified and
  settled events; observe returned payment-required results for challenged.
- [ ] Test a mocked valid payment and a handler exception. Require one verified,
  one started, one completed/failed, and one settled only on success.
- [ ] Re-run `tests.test_v17_mcp`, `tests.test_v19_discovery`, and the activity
  tests; require GREEN and unchanged tool schemas.

### Task 4: Instrument HTTP `/fetch` without observing request data

**Files:**
- Modify: `tests/test_v110_activity.py`
- Modify: `smartfetch/server.py`

- [ ] Add tests for unpaid 402, successful mocked paid execution/settlement, and
  fetch failure. Assert event stages and that the target URL never appears.
- [ ] Verify RED because HTTP lifecycle events are absent.
- [ ] Emit HTTP attempt/challenge/verified/settled from status, x402 request
  state, and standard response-header presence only.
- [ ] Emit started/completed/failed around `_run_fetch`, using error codes/status
  rather than exception text.
- [ ] Re-run HTTP payment/API tests plus activity tests and require GREEN.

### Task 5: Add runnable guarded buyer examples

**Files:**
- Create: `examples/python/paid_mcp_client.py`
- Create: `examples/typescript/paid-mcp-client.ts`
- Create: `examples/README.md`
- Create: `tests/test_v110_examples.py`

- [ ] Add tests that import the Python module without credentials/network calls,
  exercise a pure payment-client builder with a fake signer, and reject a
  challenge above `$0.005`.
- [ ] Add a TypeScript syntax/typecheck command or AST-level smoke check using a
  minimal local package setup; the check must not execute the client.
- [ ] Verify RED because example artifacts are missing.
- [ ] Implement the Python Streamable HTTP example with CDP-managed account,
  standard x402 spend controls, free tools/list, paid call, and receipt output.
- [ ] Implement the TypeScript Streamable HTTP example using CdpX402Client with
  Base-mainnet USDC per-payment/cumulative limits of 5,000 atomic units.
- [ ] Document required packages, CDP environment variables, wallet funding,
  and the fact that running either example spends real Base-mainnet USDC.
- [ ] Re-run example tests/checks and require GREEN. Do not run either client.

### Task 6: Version and documentation integration

**Files:**
- Modify: `smartfetch/config.py`
- Modify: `server.json`
- Modify: `README.md`
- Modify: version assertions in existing tests

- [ ] Update focused tests to expect `1.10.0` while preserving Registry name,
  repository, description, and remote.
- [ ] Verify RED on version expectations.
- [ ] Bump runtime/registry version and update README title, status, routes,
  observability event glossary, examples, and container tags.
- [ ] Ensure README and llms.txt link both examples and accurately label
  `/.well-known/x402` as a community discovery endpoint.
- [ ] Re-run version/registry/discovery tests and require GREEN.

### Task 7: Full regression and privacy verification

**Files:**
- Verify all changed files

- [ ] Run `python -m unittest discover -s tests -p 'test_*.py'`.
- [ ] Run `python tests/security_smoke.py` and `python tests/api_local_smoke.py`.
- [ ] Run `git diff --check` and inspect `git diff --stat`.
- [ ] Search the diff and emitted test logs for secrets, active payee, target URL,
  request body, payment signature, authorization, and IP fields.
- [ ] Confirm tools/list returns the exact four V1.9 names and every unpaid tool
  still blocks retrieval.
- [ ] Confirm `/.well-known/glama.json` behavior is unchanged.
- [ ] Review final diff against this spec. Do not deploy, push, or pay.
