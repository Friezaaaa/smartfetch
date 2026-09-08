# SmartFetch V1.11 Stage 3 Media Ingestion Implementation Plan

> Execute test-first in the isolated `feat/v1.11-media-ingestion` worktree. This stage adds internal foundations only; it must not expose routes, tools, payments, or provider calls.

**Goal:** Add privacy-safe, request-local ingestion and validation for image, PDF, audio, and video inputs, plus bounded inline accounting and a fail-closed transient provider-file lifecycle.

**Architecture:** A new internal media package owns immutable limits, finite failures, byte-signature classification, streaming download/temp-file lifecycle, format inspectors, and prepared-media decisions. It reuses `validate_public_url` at every URL/redirect boundary and injects transports, subprocess runners, retrieval callables, and provider-file clients so tests make no network/provider calls. Existing Stage 1–2 contracts and all public server surfaces remain untouched.

**Dependencies:** `Pillow==12.3.0`, `pypdf==6.18.0`, and the Debian `ffmpeg` system package providing `ffprobe`.

---

## Task 1: Lock scope, limits, and dependencies

- [ ] Add focused tests for exact media types, byte/page/pixel/frame/duration limits, finite error codes, and inline serialized-size accounting.
- [ ] Demonstrate RED before adding production modules.
- [ ] Add exact Pillow/pypdf pins and the minimal `ffmpeg` container package without changing startup.
- [ ] Implement immutable media limit/value types and bounded safe errors.
- [ ] Run the focused contract tests GREEN.

## Task 2: Streaming download and private temporary files

- [ ] Add RED tests for HTTPS-only input, initial/redirect SSRF validation, five redirects, declared/streamed oversize, slow/failed streams, false MIME claims, unpredictable names, restrictive permissions, cancellation cleanup, and concurrent isolation.
- [ ] Implement an injected async streaming transport with connect/read/wall-time limits and no trust in environment proxies.
- [ ] Validate each redirect with the existing public-target validator before following it.
- [ ] Stream into request-local OS temporary storage; never retain caller filenames or URLs.
- [ ] Guarantee cleanup in `finally` on success, finite failure, timeout, and cancellation.
- [ ] Run downloader/lifecycle tests GREEN.

## Task 3: Image and PDF inspection

- [ ] Add RED tests for valid JPEG/PNG/WebP, malformed/truncated input, spoofed MIME, exactly-one-frame enforcement, decompression/pixel cap, hostile metadata, valid PDF, encryption, malformed PDF, and 20-page boundary.
- [ ] Implement Pillow inspection with explicit format agreement, single-frame validation, 20-megapixel cap, and full decode verification.
- [ ] Implement pypdf inspection with encryption rejection and bounded page count.
- [ ] Convert all library errors to finite safe media failures without raw text or paths.
- [ ] Run image/PDF tests GREEN.

## Task 4: Audio/video ffprobe inspection

- [ ] Add RED tests for allowlisted containers/codecs, malformed or contradictory metadata, missing/non-finite/negative duration, exact duration boundaries, external references, timeout, nonzero exit, oversized stdout/stderr, and cleanup.
- [ ] Implement direct `ffprobe` execution without a shell, against only the validated local file.
- [ ] Restrict protocols, use a minimal environment, enforce the five-second timeout, and bound stdout/stderr while streaming.
- [ ] Parse only allowlisted JSON fields and emit finite safe failures.
- [ ] Run audio/video tests GREEN.

## Task 5: Prepared media and transient Files API lifecycle

- [ ] Add RED tests for complete 18,000,000-byte inline accounting including base64/schema/prompt/instructions.
- [ ] Prove images never use provider files; oversized permitted PDF/audio/video selects provider-file lifecycle.
- [ ] Add RED fake-client tests proving request-local identifiers, deletion in `finally` after success/failure/timeout/cancellation, one bounded read-back only when deletion is inconclusive, and fail-closed cleanup errors.
- [ ] Implement inert client protocols and a transient lifecycle abstraction with no provider SDK calls, credentials, retries, retained state, or background work.
- [ ] Run lifecycle tests GREEN.

## Task 6: New webpage render-mode adapter

- [ ] Add RED tests proving `auto` calls the existing retrieval engine normally and `always` maps only to `force_browser=True`.
- [ ] Implement an injected internal adapter; do not modify `core.py` or public `/fetch` behavior.
- [ ] Reject every render mode other than `auto` and `always` before work.
- [ ] Run render-mode tests GREEN.

## Task 7: Scope, security, and regression verification

- [ ] Run focused Stage 3 tests and all V1.11 Stage 1–3 tests.
- [ ] Run the complete Python suite with bundled Node 24, security smoke, API smoke, and MCP smoke.
- [ ] Confirm initialize/list are free, exactly four existing tools remain, and unpaid execution is challenged before retrieval.
- [ ] Run `pip check`, `pip-audit`, `npm ci`, lockfile audit, and TypeScript check.
- [ ] Verify Docker/container declaration and `ffprobe` availability using Docker or an equivalent available builder.
- [ ] Scan for secrets, canaries, local paths, provider network activity, and protected-file changes.
- [ ] Run `git diff --check`; remove only generated artifacts.
- [ ] Review complete base-to-head diff, commit, push, create one open PR, and stop without merge/deployment.
