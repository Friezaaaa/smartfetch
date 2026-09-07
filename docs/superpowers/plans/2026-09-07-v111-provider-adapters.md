# SmartFetch V1.11 Stage 2 Provider Adapters Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add inert, fail-closed Exa and Gemini provider adapters, benchmark-controlled model routing, exact request-local cost controls, and local circuit breakers without exposing routes, tools, payment requirements, or provider configuration.

**Architecture:** Keep the PR 1 contracts and validators unchanged. Provider adapters depend on small injected transports, translate only allowlisted provider fields into the existing immutable result types, and raise finite safe errors. A request-local integer-micro-USD budget and a process-local thread-safe circuit breaker guard every provider call. Gemini uses the official Google GenAI Interactions SDK through a narrow transport, while Exa uses the documented HTTPS Search API through the existing `requests` dependency to avoid the Exa SDK's unrelated OpenAI dependency chain.

**Tech Stack:** Python 3.12, `google-genai==2.22.0`, `requests`, `unittest`, immutable dataclasses, `asyncio`, exact integer/rational arithmetic.

---

### Task 1: Establish provider failure, readiness, and circuit contracts

**Files:**
- Create: `smartfetch/provider_health.py`
- Create: `tests/test_v111_provider_health.py`

**Steps:**
1. Add failing tests for finite error codes, secret-redacted configuration, missing configuration, closed/open/half-open circuit transitions, concurrent probe exclusion, and injected monotonic time.
2. Run `python -m unittest tests.test_v111_provider_health -v` and confirm the failure is the missing module/API.
3. Implement exact-type bounded configuration, `ProviderAdapterError`, and a lock-protected circuit breaker with no I/O or environment reads.
4. Rerun the focused test to green.

### Task 2: Add exact request-local spend controls and provider cost calculations

**Files:**
- Create: `smartfetch/provider_controls.py`
- Create: `tests/test_v111_provider_controls.py`

**Steps:**
1. Add failing tests for reservations, concurrent overspend prevention, release/commit behavior, exact Exa decimal conversion, Gemini rational token rates, rounding upward, negative/non-finite/overflow rejection, and no double-counting of modality totals.
2. Confirm RED because the controls do not exist.
3. Implement a request-owned locked budget, opaque reservation tokens, per-provider ceilings, and integer/rational cost functions returning the merged `ProviderUsage` type.
4. Rerun the focused test to green.

### Task 3: Add benchmark-controlled two-model routing

**Files:**
- Create: `smartfetch/model_routing.py`
- Create: `tests/test_v111_model_routing.py`

**Steps:**
1. Add failing tests proving the candidate set is exactly `gemini-3.8-flash` and `gemini-3.5-flash-lite`, every designed workload is finite, no default winner exists, incomplete/untrusted tables fail closed, only approved overrides work, and selections carry bounded output tokens plus low thinking effort.
2. Confirm RED.
3. Implement an immutable routing table and selection result with no environment reads and no caller-selected model field.
4. Rerun the focused test to green.

### Task 4: Add the Exa Search API adapter

**Files:**
- Create: `smartfetch/providers/__init__.py`
- Create: `smartfetch/providers/exa.py`
- Create: `tests/test_v111_exa_adapter.py`

**Steps:**
1. Add failing tests for all three modes, exact endpoint/body/header shape, bounded highlights, domains/freshness/counts, no retry, strict timeouts, response-byte cap, finite HTTP/network/timeout errors, cost extraction, result normalization, URL/result bounds, canary redaction, readiness/circuit/budget gating before transport, and injected transport call counts.
2. Confirm RED.
3. Implement an async adapter over an injected transport and a default streaming `requests` transport. Parse JSON with `Decimal`, discard provider-only fields, assign opaque source IDs locally, and return only `SearchProviderResult`.
4. Rerun the focused test to green.

### Task 5: Add the stateless Gemini Interactions adapter

**Files:**
- Create: `smartfetch/providers/gemini.py`
- Create: `tests/test_v111_gemini_adapter.py`
- Modify: `requirements.txt`

**Steps:**
1. Add failing tests for answer, structured text, and prepared-media paths; actual kwargs at the mocked SDK boundary; explicit `store=False`; absent `previous_interaction_id` and `background`; `stream=False`; empty tools; server-selected model only; bounded output tokens; low thinking effort; timeout/no retry; raw-output caps; usage normalization; finite errors; citation/source checks; schema-shaped output; no secret/canary leakage; readiness/circuit/budget gating; and zero network calls.
2. Confirm RED.
3. Pin `google-genai==2.22.0`, the first current stable Interactions-capable release verified compatible with existing pins. Do not add `exa-py` because the documented HTTPS API is sufficient and avoids its OpenAI transitive dependency.
4. Implement the official SDK transport and injected test transport. The adapter must construct one non-streaming stateless interaction, never enable tools, continuation, or background work, and expose only normalized PR 1 result types.
5. Rerun the focused test to green.

### Task 6: Integration, scope, and safety regression tests

**Files:**
- Create: `tests/test_v111_provider_stage2_scope.py`

**Steps:**
1. Add tests proving absent configuration and open circuits call neither transport, no provider module imports server/payment/retrieval modules, no environment accessor is used, no public route/tool count changes, and all adapter failures contain only finite error text.
2. Run all Stage 2 focused tests and fix only Stage 2 defects.
3. Inspect the base-to-head diff and confirm protected payment, settlement, retrieval, browser, SSRF, MCP, discovery, server, pricing, version, Docker, and TypeScript files have zero diff.

### Task 7: Full verification and delivery

**Files:** No production changes expected.

**Steps:**
1. Run the complete Python suite with the bundled Node 24 runtime.
2. Run security, API, and MCP smoke tests; confirm free initialize/list, exactly four unchanged tools, and unpaid execution challenged before retrieval.
3. Run dependency resolution, `pip check`, and `pip-audit`.
4. Run `npm ci`, lockfile-only audit, and `npm run check`.
5. Run secret/canary/provider-network and protected-file scans, `git diff --check`, and remove only generated caches/artifacts.
6. Review the complete diff, stage only the approved files, commit, push `feat/v1.11-provider-adapters`, and create one open PR with RED/GREEN and gate evidence.

## Scope verification

- No REST route, MCP wrapper, public tool, payment requirement, discovery entry, media downloader, Files API upload, `ffprobe`, version bump, or live documentation is added.
- No Exa/Gemini request is made by tests; every adapter transport is injected or intercepted.
- No API key is read from the environment or accepted from public contracts. Configuration is server-injected and secret-redacted.
- The merged contracts, schema guard, evidence validator, provider protocols, and current four MCP tools remain the fixed interfaces.
