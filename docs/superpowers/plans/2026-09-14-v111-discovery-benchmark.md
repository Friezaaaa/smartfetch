# SmartFetch V1.11 Stage 5 Discovery and Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare conditional V1.11 discovery and version metadata and add an offline-default, fail-closed 32-case benchmark harness without enabling production or contacting providers.

**Architecture:** Extend the existing restart-only activation snapshot into discovery generation, deriving all advertised variants, prices, resources, and payment fields from `V111_VARIANTS` and generated `PaymentRequirements`. Keep benchmark case loading, authorization, execution injection, validation, and reporting in a separate internal module and thin CLI so offline validation never reads credentials or creates provider clients.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, x402 2.20.0, existing V1.11 immutable contracts, JSON/Markdown benchmark reports, local Pillow/PDF generation, and temporary portable eSpeak NG/FFmpeg fixture tooling.

**Spec:** `docs/superpowers/specs/2026-09-02-v111-search-multimodal-design.md`

## Global Constraints

- Disabled or incomplete activation exposes zero V1.11 REST discovery paths and exactly four legacy MCP tools.
- Complete injected activation exposes exactly eight fixed REST variants and six MCP tools.
- Variant prices and resource identities derive from `V111_VARIANTS` and generated runtime payment requirements.
- Service and manifest metadata become `1.11.0`, while repository documentation states the open PR is not deployed, enabled, benchmark-approved, or Registry-published.
- The benchmark has exactly 32 logical cases, 12 Exa searches, and 56 Gemini trials; it never retries.
- Offline mode is the default and must not read credentials or create network/provider clients.
- Real mode requires the exact CLI/environment approval tuple and an ASCII integer budget of 1–10,000,000 micro-USD.
- No provider call, credential use, benchmark execution, production activation, payment, deployment, or publication occurs in this task.
- Existing `/fetch`, legacy MCP contracts, payment behavior, retrieval, media, SSRF, pricing, and settlement remain unchanged.

---

### Task 1: Conditional discovery context and runtime-derived payment metadata

**Files:**
- Modify: `smartfetch/discovery.py`
- Modify: `smartfetch/bazaar.py`
- Modify: `smartfetch/server.py`
- Test: `tests/test_v111_stage5_discovery.py`

**Interfaces:**
- Consumes: `V111Activation`, `V111_VARIANTS`, generated HTTP/MCP `PaymentRequirements`.
- Produces: conditional `openapi_document`, `x402_manifest`, `docs_html`, `llms_text`, root/`meta` capability metadata.

- [ ] Add tests showing disabled and incomplete activation produce zero V1.11 paths/resources/capabilities and four tools.
- [ ] Run the focused tests and verify RED because discovery currently ignores activation.
- [ ] Add immutable discovery context assembled by `create_app` from the activation and generated requirements.
- [ ] Add tests proving enabled discovery contains exactly eight REST paths, eight HTTP and eight MCP payment identities, six tools, and runtime-derived amounts/network/asset/payee.
- [ ] Implement the smallest conditional generators and rerun focused tests GREEN.
- [ ] Add request/response/payment-schema tests for all eight paths, no wildcard, and unchanged `POST /fetch`; rerun GREEN.

### Task 2: Human and agent documentation plus release metadata

**Files:**
- Modify: `smartfetch/config.py`
- Modify: `smartfetch/discovery.py`
- Modify: `README.md`
- Modify: `server.json`
- Modify: version-dependent tests
- Test: `tests/test_v111_stage5_documentation.py`

**Interfaces:**
- Consumes: Task 1 conditional discovery context.
- Produces: concise conditional HTML/`llms.txt`, consistent `1.11.0` metadata, unreleased-status wording.

- [ ] Add RED tests for version consistency, concise valid Markdown links, eight differentiated variants, limits/evidence/nullability/deadlines/payment-header/no-settlement language, and forbidden unsupported claims.
- [ ] Bump approved metadata to `1.11.0`, update `server.json`, README, docs, root metadata, and assertions without claiming deployment/publication/benchmark approval.
- [ ] Rerun documentation tests GREEN and parse every generated JSON document.

### Task 3: Exact benchmark manifest and offline fixtures

**Files:**
- Create: `benchmarks/v111/cases.json`
- Create: `benchmarks/v111/fixtures/*`
- Create: `benchmarks/v111/fixtures.json`
- Create: `scripts/generate_v111_benchmark_fixtures.py`
- Create: `tests/test_v111_stage5_manifest.py`

**Interfaces:**
- Produces: exact 32-case manifest; under-8-MiB offline image/PDF/audio/video corpus with byte sizes and SHA-256.

- [ ] Add RED tests for exact IDs, four cases per variant, corrected search count fields, strict schemas/oracles, nullable fields, fixture paths, case ordering, and expected-failure nulls.
- [ ] Implement deterministic manifest data and verify tests GREEN.
- [ ] Generate non-sensitive assets offline only; use explicitly supplied portable eSpeak NG and FFmpeg paths for narrated audio/video, preserve completed image/PDF fixtures, and validate every asset through the real Stage 3 media boundary.
- [ ] Add fixture hash/size/MIME/corpus-size tests and rerun GREEN.

### Task 4: Offline-default benchmark core and real-mode authorization

**Files:**
- Create: `smartfetch/benchmark_v111.py`
- Create: `scripts/benchmark_v111.py`
- Test: `tests/test_v111_stage5_benchmark.py`

**Interfaces:**
- Produces: `load_benchmark_manifest`, `authorize_real_run`, injected `BenchmarkExecutor`, bounded trial/result records, JSON and Markdown renderers.
- Consumes: existing model IDs, cost accounting, provider usage, schema/evidence/citation validators.

- [ ] Add RED tests proving default execution validates locally without reading environment credentials or calling providers.
- [ ] Implement exact plain-JSON manifest validation, fixture hash validation, fixed alternating model order, immutable shared-input packages, and no-retry injected execution.
- [ ] Add RED tests for missing/partial credentials, flag/approval/budget mismatch, non-ASCII/malformed/out-of-range budget, production-enabled flag, unexpected model/provider/case/hash, and calculated-maximum reservation.
- [ ] Implement fail-closed authorization before executor construction and rerun GREEN.
- [ ] Add cost/accounting tests proving exact integer micro-USD, mandatory provider usage, per-operation reservation, cumulative ceiling, 12 Exa searches, 56 Gemini trials, and failure preservation; rerun GREEN.

### Task 5: Bounded privacy-safe benchmark reporting

**Files:**
- Modify: `smartfetch/benchmark_v111.py`
- Modify: `scripts/benchmark_v111.py`
- Test: `tests/test_v111_stage5_benchmark.py`

**Interfaces:**
- Produces: bounded machine-readable JSON and concise Markdown containing only the approved measurement allowlist and aggregates.

- [ ] Add RED tests with source/provider/credential/canary payloads proving reports omit them and never label a statistic P95.
- [ ] Implement exact-type bounded report normalization, aggregate median/maximum/success/cost totals, deterministic serialization, and finite errors.
- [ ] Add tests proving invalid/partial/schema-invalid/evidence-invalid/citation-invalid results are never counted as successful delivery; rerun GREEN.

### Task 6: Complete verification and delivery

**Files:** Review all base-to-head changes; no additional production scope.

- [ ] Run focused Stage 5 tests and all V1.11 tests.
- [ ] Run the complete Python suite with bundled Node 24, security 7/7, disabled API/MCP smoke, enabled discovery/tool-count tests, and unpaid legacy gating.
- [ ] Run `pip check`, `pip-audit -r requirements.txt`, `npm ci`, lockfile audit, and `npm run check`.
- [ ] Run secret/provider-network/protected-file scans, parse JSON/Markdown outputs, prove zero real provider calls/credentials, and run `git diff --check`.
- [ ] Remove only generated caches, review the complete diff, stage only Stage 5 files, commit, push, create one open PR, and stop.

## Self-review

- Coverage: conditional discovery, release metadata, every approved case and fixture, exact real-mode authorization, accounting, privacy-safe reporting, and all requested gates map to Tasks 1–6.
- Scope: no contract-limit change, provider adapter change, production activation, payment behavior change, live call, deployment, or publication is included.
- Interfaces: discovery consumes existing activation/payment objects; benchmark execution is injected and cannot construct providers before authorization.
- Placeholders: none; every task names concrete files, behavior, RED/GREEN commands, and outputs.
