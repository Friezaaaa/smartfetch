# SmartFetch V1.11 Paid Interfaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the disabled-by-default Stage 4 orchestration and eight statically priced REST/MCP payment integrations without releasing V1.11 or changing existing SmartFetch behavior.

**Architecture:** A restart-only activation object validates the exact global flag and injected provider controls once during application construction. A request-local orchestration service composes the merged Exa, Gemini, retrieval, media, schema, evidence, cost, and circuit interfaces; thin REST and MCP wiring validates input and takes non-consuming readiness snapshots before entering one exact official x402 wrapper per variant.

**Tech Stack:** Python 3.13, FastAPI/Starlette, MCP FastMCP, x402 2.20.0, Pydantic 2, existing Stage 1–3 SmartFetch modules, unittest with mocked transports.

**Spec:** `docs/superpowers/specs/2026-09-02-v111-search-multimodal-design.md`

## Global Constraints

- `SMARTFETCH_V111_ENABLED` enables only for exact lowercase `true`; all other values fail closed without logging the value.
- Activation is all-or-nothing and restart-only: zero or eight REST routes, and exactly four or six MCP tools.
- Static variants are the eight `V111_VARIANTS` definitions and prices `$0.05`, `$0.10`, and `$0.15`; no body-derived or shared cheaper requirement.
- Bounded input and non-consuming circuit readiness precede x402; real single-use permits are acquired only inside merged provider adapters after verification.
- Existing `/fetch`, four MCP tools, payment settings, retrieval, SSRF, media, provider adapters, version `1.10.6`, and discovery output remain unchanged.
- Automated tests use injected fakes and mocked payment/provider boundaries; no credential reads, provider calls, wallet operations, or payments.

---

### Task 1: Restart-only activation contract

**Files:**
- Create: `smartfetch/v111_service.py`
- Create: `tests/test_v111_stage4_activation.py`
- Modify: `docs/superpowers/specs/2026-09-02-v111-search-multimodal-design.md`

**Interfaces:**
- Produces: `load_v111_activation(environ, runtime) -> V111Activation`
- Produces: immutable `V111Activation(enabled, service)` and `V111RuntimeConfig`
- Consumes: `ProviderConfig`, `ProviderCircuitBreaker`, `BenchmarkModelRouter`

- [ ] **Step 1: Write failing flag and activation tests**

```python
def test_only_exact_lowercase_true_can_activate(self):
    for value in (None, "", "false", "TRUE", " true ", "1"):
        self.assertFalse(load_v111_activation(env(value), complete_runtime()).enabled)

def test_complete_injected_runtime_activates_atomically(self):
    activation = load_v111_activation(
        {"SMARTFETCH_V111_ENABLED": "true"}, complete_runtime()
    )
    self.assertTrue(activation.enabled)
```

- [ ] **Step 2: Run tests and verify RED because the activation API is absent**

Run: `python -m unittest tests.test_v111_stage4_activation -v`

- [ ] **Step 3: Implement exact parsing, bounded warning, and local validation**

```python
def load_v111_activation(environ, runtime):
    raw = environ.get("SMARTFETCH_V111_ENABLED")
    if raw not in {None, "", "false", "true"}:
        emit_configuration_warning("v111_config_invalid")
        return V111Activation.disabled()
    if raw != "true" or runtime is None or not runtime.validate_local():
        return V111Activation.disabled()
    return V111Activation.enabled_with(runtime.build_service())
```

- [ ] **Step 4: Run focused tests and verify GREEN**

### Task 2: Request-local orchestration

**Files:**
- Create: `smartfetch/v111_service.py`
- Create: `tests/test_v111_stage4_service.py`

**Interfaces:**
- Produces: `V111Service.require_ready(variant)` with no permit consumption
- Produces: `V111Service.execute_search(request)` and `execute_extraction(request)`
- Consumes: `SearchAndExtractRequest`, `DirectExtractionRequest`, merged provider/result types, `retrieve_webpage`, `ingest_remote_media`, `choose_delivery`

- [ ] **Step 1: Add RED tests for the three search and five extraction flows**

```python
async def test_answer_searches_retrieves_then_synthesizes(self):
    result = await service.execute_search(valid_answer_request())
    self.assertEqual(result.mode, "answer")
    self.assertEqual(fake.calls, ["exa", "retrieve:s1", "gemini:answer"])

async def test_media_is_validated_before_inline_model_call(self):
    result = await service.execute_extraction(valid_image_request())
    self.assertEqual(result.source_type, "image")
    self.assertEqual(fake.calls, ["ingest:image", "gemini:image"])
```

- [ ] **Step 2: Verify RED because orchestration does not exist**

- [ ] **Step 3: Implement request-local factories, bounded source registries, public response construction, and finite failure mapping**

```python
async def execute_search(self, request):
    budget = self._runtime.new_budget()
    search = self._runtime.new_exa(request.mode, budget)
    candidates = await search.search(to_search_request(request))
    if request.mode == "results":
        return build_results_response(request, candidates)
    sources = await self._retrieve_selected(candidates, request)
    model = self._runtime.new_gemini(workload(request), budget)
    return await synthesize_and_validate(model, request, sources)
```

- [ ] **Step 4: Add RED/GREEN tests for finite errors, invalid evidence, all-null output, timeouts, and concurrency isolation**

### Task 3: Eight static REST payment resources

**Files:**
- Create: `smartfetch/v111_payments.py`
- Create: `tests/test_v111_stage4_rest.py`
- Modify: `smartfetch/payments.py`
- Modify: `smartfetch/server.py`

**Interfaces:**
- Produces: immutable static `RouteConfig` mapping for all eight exact `POST` paths
- Produces: pre-x402 middleware for body validation and non-consuming readiness
- Consumes: existing `create_x402_resource_server()` and official FastAPI payment middleware

- [ ] **Step 1: Add RED tests for disabled 404, enabled eight-route registration, exact unpaid challenges, and input/readiness before challenge**

```python
def test_each_enabled_route_has_exact_static_challenge(self):
    for path, expected_amount in EXPECTED.items():
        response = client.post(path, json=valid_body(path))
        self.assertEqual(response.status_code, 402)
        self.assertEqual(decode(response)["accepts"][0]["amount"], expected_amount)

def test_invalid_input_and_open_circuit_are_free(self):
    self.assertEqual(client.post(PATH, json={}).status_code, 400)
    circuit.open_for_test()
    self.assertEqual(client.post(PATH, json=VALID).status_code, 503)
    verifier.assert_not_awaited()
```

- [ ] **Step 2: Verify RED against the existing 404 behavior**

- [ ] **Step 3: Build eight literal route configs from `V111_VARIANTS`, using active network/payee and each variant price**

```python
routes[f"POST {variant.rest_path}"] = RouteConfig(
    accepts=PaymentOption(
        scheme="exact", pay_to=settings.pay_to,
        price=variant.price, network=settings.network,
    ),
    description=description_for(variant),
    mime_type="application/json",
    service_name="SmartFetch",
)
```

- [ ] **Step 4: Register thin handlers only when activation is enabled and place validation/readiness outside the x402 middleware**

- [ ] **Step 5: Add RED/GREEN settlement tests proving success settles once and every status 400+ path does not settle**

### Task 4: Eight static MCP wrappers behind two public tools

**Files:**
- Create: `tests/test_v111_stage4_mcp.py`
- Modify: `smartfetch/mcp_server.py`
- Modify: `smartfetch/v111_payments.py`

**Interfaces:**
- Produces: immutable wrapper maps keyed by three `SearchMode` and five `SourceType` values
- Registers: `search_and_extract` and `extract_structured_data` only under active startup configuration

- [ ] **Step 1: Add RED tests for four disabled tools, six enabled tools, free initialize/list, and all eight variant challenges**

```python
async def test_enabled_tools_are_atomic(self):
    tools = await session.list_tools()
    self.assertEqual([tool.name for tool in tools.tools], EXPECTED_SIX)

async def test_invalid_discriminator_returns_free_input_error(self):
    result = await session.call_tool("search_and_extract", {"mode": "unknown"})
    self.assertTrue(result.isError)
    self.assertNotIn("accepts", result.structuredContent or {})
```

- [ ] **Step 2: Verify RED because only four tools exist**

- [ ] **Step 3: Prebuild eight official `create_payment_wrapper()` instances with exact resources and prices, then dispatch only after local validation/readiness**

- [ ] **Step 4: Add RED/GREEN tests for cheaper authorization rejection, race-to-open circuit, one permit per provider call, no settlement on error, and concurrent permit isolation**

### Task 5: Privacy, compatibility, and scope regression

**Files:**
- Create: `tests/test_v111_stage4_scope.py`
- Modify: `smartfetch/activity.py` only if a failing activity test requires finite Stage 4 categorical fields

**Interfaces:**
- Protects: existing `/fetch`, four MCP contracts, payment settings, discovery output, version, providers, retrieval, media, SSRF, and buyer examples

- [ ] **Step 1: Add RED/GREEN integration tests proving no query/schema/source/provider payload/payment data reaches logs**

- [ ] **Step 2: Prove no provider network can occur before verified handler entry by using transports that raise on invocation**

- [ ] **Step 3: Confirm default application has no Stage 4 route/tool/discovery changes and production remains version `1.10.6`**

### Task 6: Verification, review, commit, and PR

**Files:**
- Review every base-to-head file; remove only generated worktree artifacts.

- [ ] **Step 1: Run focused Stage 4 and all V1.11 suites**
- [ ] **Step 2: Run the complete Python suite with bundled Node 24, security/API/MCP smokes, payment-gating probes, and exact tool counts**
- [ ] **Step 3: Run `pip check`, `pip-audit`, `npm ci`, lockfile audit, and TypeScript check**
- [ ] **Step 4: Run secret/provider-network/protected-file scans and `git diff --check`**
- [ ] **Step 5: Review diff for only Stage 4 orchestration, static payment wiring, activation documentation, and tests**
- [ ] **Step 6: Commit, push `feat/v1.11-paid-interfaces`, create one PR, and leave it open**
