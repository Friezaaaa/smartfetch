# SmartFetch V1.11.1 — emergency fail-closed mode

This source tree is an emergency containment release. Retrieval is temporarily unavailable. Browser rendering is temporarily unavailable. Every valid `POST /fetch` request and every invocation of the four existing MCP tools fails before payment verification, DNS, network retrieval, provider work or settlement. There is no payment challenge for an unavailable operation.

V1.11 paid capabilities remain disabled: the eight V1.11 REST variants return ordinary 404 responses, and the two additional MCP tools are absent. The V1.11 Stage 5 benchmark remains offline by default; this release does not authorize a real provider benchmark or activate Exa or Gemini.

This repository version is **1.11.1**. It does not claim that a V1.11.1 image, Official MCP Registry entry or x402scan listing has been published. V1.10.6 remains the separately published Official MCP Registry version; publication of this emergency version requires separate review and authorization. Deployment, production configuration, Registry publication and x402scan changes are separate actions.

## Current public contract

- `GET /health` remains a free liveness route. Its JSON includes `version: "1.11.1"`, `retrieval_available: false`, `browser_available: false` and `mode: "emergency_fail_closed"`.
- `/`, `/docs`, `/openapi.json`, `/llms.txt`, `/.well-known/x402`, `/meta`, `/robots.txt` and `/sitemap.xml` remain free. They identify retrieval as unavailable and advertise zero actionable payable resources.
- Valid `POST /fetch` returns finite HTTP 503 `retrieval_unavailable`. A valid forced-browser request instead returns finite HTTP 503 `browser_unavailable`. Both include `Retry-After: 3600` and `Cache-Control: no-store` and do not process payment headers.
- Malformed, oversized or unsupported request envelopes retain finite validation errors rather than being relabeled as unavailable.
- MCP `initialize` and `tools/list` remain free. `tools/list` exposes exactly `fetch_webpage`, `webpage_to_markdown`, `extract_webpage_text` and `render_webpage`, each explicitly marked unavailable. Calling any of them returns a finite error result before a payment wrapper or retrieval handler.
- The eight V1.11 REST variants remain 404, and `search_and_extract` and `extract_structured_data` are absent from MCP discovery.

Successful retrieval responses do not exist in this emergency mode. The additive `content_trust: untrusted_external` marker is reserved for future successful retrieved results; it is not inserted into emergency errors and does not rewrite source content.

## Local source validation

Use an isolated Python environment with the repository's declared dependencies and run the emergency test suites before building. The local container build command is:

```sh
docker build -t smartfetch:v1.11.1 .
```

This is a **local image tag**, not a remote published V1.11.1 image. The Dockerfile runs as UID/GID `10001:10001` and does not install Chromium, FFmpeg or FFprobe. The built-image gate runs the emergency API/MCP smoke script inside a locally started container. No provider, facilitator, wallet or payment credentials are required for startup or these free checks.

## Release and rollback boundary

Do not activate V1.11 paid interfaces or restore retrieval while the approved SSRF, cancellation, credential-isolation and cleanup boundaries remain unproven. A security rollback may use only another verified fail-closed image; restoring a retrieval-enabled V1.11.0 image is prohibited. Browser restoration and any retrieval-enabled architecture require separate exact-runtime and final-image containment gates. Publishing to the Official MCP Registry or x402scan requires separate authorization after deployment verification.

Historical V1.10 payment/retrieval details remain available in earlier Git history. They are not instructions to pay or execute retrieval against this emergency service.
