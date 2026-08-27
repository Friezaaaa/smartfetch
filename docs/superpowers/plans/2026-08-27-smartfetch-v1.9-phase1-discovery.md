# SmartFetch V1.9 Phase 1 Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand SmartFetch to four paid MCP tool views and add five free, proxy-aware public discovery routes without changing retrieval or x402 behavior.

**Architecture:** Keep explicit typed FastMCP tool functions, but share their payment wrapping, Bazaar declarations, and result projection. Put all human/crawler discovery document generation in a new pure `smartfetch/discovery.py` module; `smartfetch/server.py` only derives the normalized proxy-aware public base URL from Starlette's request URL and wires free routes.

**Tech Stack:** Python 3.12, FastAPI 0.141.1, Starlette, Uvicorn 0.52.4, MCP 1.29.0, x402 2.20.0, CDP SDK 1.47.1, Pydantic, unittest, FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-08-27-smartfetch-v1.9-phase1-discovery-design.md`

## Global Constraints

- Preserve `smartfetch/core.py`, extraction, browser fallback, SSRF protections, concurrency, timeouts, output limits, and retry behavior unchanged.
- Preserve existing `POST /fetch`, HTTP Bazaar discovery, x402 exact payment behavior, `$0.005` default price, Base Sepolia, Base mainnet, CDP facilitator, and `X402_PAY_TO` behavior.
- Keep MCP Streamable HTTP at exactly `/mcp`; MCP tool handlers call the internal retrieval handler, never public `/fetch`.
- Expose exactly four MCP tools: `fetch_webpage`, `webpage_to_markdown`, `extract_webpage_text`, and `render_webpage`.
- Keep initialize, tools/list, `/`, `/health`, `/meta`, `/docs`, `/openapi.json`, `/llms.txt`, `/robots.txt`, and `/sitemap.xml` free.
- Build runtime public URLs only from Starlette's proxy-aware `request.url.scheme` and `request.url.netloc`, normalized to `scheme://host`; never parse forwarded headers manually.
- Keep the fixed Railway `/mcp` URL only in `server.json`.
- Do not change dependency versions, paid-client scripts, Railway configuration, or deployment files.
- Do not publish, deploy, seed Bazaar, make a payment, commit, or push during this implementation run.

## File structure

- Create `smartfetch/discovery.py`: pure public-base normalization and HTML, OpenAPI, llms, robots, sitemap document builders.
- Create `tests/test_v19_discovery.py`: focused four-tool, payment/Bazaar, public discovery, proxy URL, and registry-manifest tests.
- Modify `smartfetch/bazaar.py`: shared per-tool schemas/examples and generalized official MCP Bazaar builder.
- Modify `smartfetch/mcp_server.py`: four explicit typed tools, shared result projection, one payment initialization, four wrappers/resources.
- Modify `smartfetch/server.py`: free discovery routes and compatible `/meta` additions.
- Modify `smartfetch/config.py`: `SERVICE_VERSION = '1.9.0'` only.
- Modify `server.json`: V1.9 version/description while preserving name/repository/one fixed remote.
- Modify `README.md`: V1.9 capabilities, discovery routes/search language, and V1.8 Registry publication status.
- Modify `tests/test_v13_api.py`, `tests/test_v14_payments.py`, `tests/test_v15_bazaar.py`, `tests/test_v17_mcp.py`, `tests/test_v18_discovery.py`, and `tests/api_local_smoke.py`: only assertions necessarily affected by version, free routes, four tools, or manifest wording.
- Modify `tests/mcp_local_smoke.py`: expect all four free-listed tools while keeping the one unpaid `fetch_webpage` challenge and no-retrieval assertion.
- Leave `requirements.txt`, `smartfetch/payments.py`, protected retrieval modules, and paid smoke scripts unchanged.

---

### Task 1: Specify the four MCP and Bazaar contracts in failing tests

**Files:**
- Create: `tests/test_v19_discovery.py`
- Read: `smartfetch/bazaar.py`
- Read: `smartfetch/mcp_server.py`

**Interfaces:**
- Consumes: existing `server.create_app`, MCP JSON-RPC helpers, `X402Settings`, and official x402 Bazaar validator.
- Produces: executable contract for tool names, descriptions, schemas, resources, challenges, and projections.

- [ ] **Step 1: Add exact shared test fixtures**

Define these literals in `tests/test_v19_discovery.py`:

```python
TOOL_NAMES = [
    'fetch_webpage',
    'webpage_to_markdown',
    'extract_webpage_text',
    'render_webpage',
]
TOOL_DESCRIPTIONS = {
    'fetch_webpage': bazaar.FETCH_DESCRIPTION,
    'webpage_to_markdown': (
        'Convert a public webpage or URL into clean Markdown for AI agents, '
        'with core retrieval metadata.'
    ),
    'extract_webpage_text': (
        'Extract clean readable text from a public webpage or URL for AI '
        'agents, with core retrieval metadata.'
    ),
    'render_webpage': (
        'Browser-render a public JavaScript-heavy webpage or URL, then return '
        'clean text, Markdown, links, and metadata.'
    ),
}
```

Use the existing mocked Base Sepolia `SupportedResponse` and a valid public
test payee. Reuse the existing MCP JSON-RPC shape from `tests/test_v18_discovery.py`.

- [ ] **Step 2: Test exact `tools/list` contracts**

With payments disabled, initialize the real mounted MCP app and assert:

```python
assert [tool['name'] for tool in tools] == TOOL_NAMES
assert {tool['name']: tool['description'] for tool in tools} == TOOL_DESCRIPTIONS
```

Assert `fetch_webpage`, `webpage_to_markdown`, and `extract_webpage_text` expose
`url`, `max_chars`, and `force_browser`; assert `render_webpage` exposes only
`url` and `max_chars`. For every `max_chars`, require default 20000, minimum
1000, and maximum 50000. For every optional browser input, require default
false. Require only `url`.

- [ ] **Step 3: Test four wrappers and unique resources**

Wrap the real `x402.mcp.create_payment_wrapper` with a mock while creating a
paid app. Assert four calls, shared `resource_server`, shared `accepts`, and
resources exactly:

```python
[
    'mcp://tool/fetch_webpage',
    'mcp://tool/webpage_to_markdown',
    'mcp://tool/extract_webpage_text',
    'mcp://tool/render_webpage',
]
```

For every call, validate `extensions['bazaar']` with
`validate_discovery_extension_spec`, and require type `mcp`, matching
`toolName`, matching description, and transport `streamable-http`.

- [ ] **Step 4: Test every unpaid challenge before retrieval**

Patch `smartfetch.server._run_fetch` as an `AsyncMock`. Call all four tools
without payment and require four `Payment Required` tool results. For each,
assert scheme `exact`, network equal to settings, payee equal to settings,
amount `5000`, matching unique resource, and matching Bazaar tool metadata.
Finally require:

```python
fetch.assert_not_awaited()
```

- [ ] **Step 5: Test internal execution and projection semantics with payments disabled**

Patch `_run_fetch` to return a complete result containing distinct `content`
and `markdown`. Call each tool once through MCP and assert:

```python
# fetch_webpage
body['content'] == 'clean text'
body['markdown'] == '# clean markdown'

# webpage_to_markdown
body['markdown'] == '# clean markdown'
'content' not in body

# extract_webpage_text
body['content'] == 'clean text'
'markdown' not in body

# render_webpage
body['content'] == 'clean text'
body['markdown'] == '# clean markdown'
```

Require `_run_fetch` arguments respectively:

```python
('https://example.com/', False, 20000)
('https://example.com/', False, 20000)
('https://example.com/', False, 20000)
('https://example.com/', True, 20000)
```

This proves internal reuse and prevents HTTP `/fetch` double charging.

- [ ] **Step 6: Run the MCP-focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_v19_discovery -v
```

Expected: FAIL because three tool names, generalized Bazaar metadata, and
projection behavior do not exist.

### Task 2: Generalize Bazaar metadata without changing the HTTP listing

**Files:**
- Modify: `smartfetch/bazaar.py`
- Test: `tests/test_v19_discovery.py`
- Test: `tests/test_v15_bazaar.py`
- Test: `tests/test_v18_discovery.py`

**Interfaces:**
- Produces: `mcp_discovery_extension(*, tool_name, description, input_schema, input_example, output_schema, output_example, transport='streamable-http') -> dict`.
- Preserves: `fetch_discovery_extension() -> dict` and `fetch_mcp_discovery_extension() -> dict`.
- Produces: per-tool input/output schema and example mappings consumed by `mcp_server.py`.

- [ ] **Step 1: Extract reusable property fragments**

Define independent dictionaries for URL, character limit, browser flag, and
common output properties. Build schemas with fresh nested dictionaries so no
caller can mutate another tool's declaration. Keep `FETCH_INPUT_SCHEMA`,
`FETCH_INPUT_EXAMPLE`, `FETCH_OUTPUT_SCHEMA`, and `FETCH_OUTPUT_EXAMPLE`
semantically unchanged except their service version later becomes `1.9.0`.

- [ ] **Step 2: Add the exact projection schemas/examples**

Add constants:

```python
MARKDOWN_DESCRIPTION = (
    'Convert a public webpage or URL into clean Markdown for AI agents, '
    'with core retrieval metadata.'
)
TEXT_DESCRIPTION = (
    'Extract clean readable text from a public webpage or URL for AI agents, '
    'with core retrieval metadata.'
)
RENDER_DESCRIPTION = (
    'Browser-render a public JavaScript-heavy webpage or URL, then return '
    'clean text, Markdown, links, and metadata.'
)
```

Define `STANDARD_INPUT_SCHEMA`/example for the first three tools and
`RENDER_INPUT_SCHEMA`/example without `force_browser`. Define Markdown output
with common result fields plus `markdown`, text output with common result
fields plus `content`, and render output as the existing full output schema and
example. `fallback_reason` remains optional.

- [ ] **Step 3: Implement the generalized official x402 builder**

Implement:

```python
def mcp_discovery_extension(
    *,
    tool_name,
    description,
    input_schema,
    input_example,
    output_schema,
    output_example,
    transport='streamable-http',
):
    return declare_mcp_discovery_extension(DeclareMcpDiscoveryConfig(
        tool_name=tool_name,
        description=description,
        transport=transport,
        input_schema=input_schema,
        example=input_example,
        output=OutputConfig(example=output_example, schema=output_schema),
    ))
```

Keep `fetch_mcp_discovery_extension()` as a thin call to this helper using the
original fetch constants. Do not change `fetch_discovery_extension()`.

- [ ] **Step 4: Add HTTP Bazaar non-regression assertions**

In `tests/test_v19_discovery.py`, inspect an unpaid `POST /fetch` at
`https://agent.example` and assert type `http`, method `POST`, resource URL
`https://agent.example/fetch`, original input/output schemas, exact scheme,
amount `5000`, configured network, and configured payee. Assert its URL differs
from all four `mcp://tool/...` resources.

- [ ] **Step 5: Run the completed Bazaar builder slice and require GREEN**

Run:

```powershell
python -m unittest tests.test_v15_bazaar tests.test_v18_discovery.V18MCPBazaarMetadataTests tests.test_v19_discovery.V19BazaarBuilderTests -v
```

Expected: all selected HTTP, legacy MCP, and generalized per-tool Bazaar
builder assertions pass. The separate MCP integration cases written in Task 1
remain RED until Task 3 and are not part of this intermediate slice.

### Task 3: Register four explicit MCP tools behind shared payment requirements

**Files:**
- Modify: `smartfetch/mcp_server.py`
- Test: `tests/test_v17_mcp.py`
- Test: `tests/test_v18_discovery.py`
- Test: `tests/test_v19_discovery.py`

**Interfaces:**
- Produces: `MCP_TOOLS: tuple[str, ...]` in exact public order.
- Preserves: `MCP_TOOL = 'fetch_webpage'` as the legacy primary-tool constant.
- Consumes: per-tool descriptions, schemas, examples, and generalized Bazaar builder.

- [ ] **Step 1: Add exact tool constants and projection helper**

Keep `MCP_TOOL` and add:

```python
MCP_TOOLS = (
    'fetch_webpage',
    'webpage_to_markdown',
    'extract_webpage_text',
    'render_webpage',
)
COMMON_PROJECTION_FIELDS = (
    'success', 'requested_url', 'final_url', 'status_code', 'render_method',
    'elapsed_ms', 'fallback_reason', 'title', 'truncated', 'max_chars',
    'request_id', 'service_version',
)

def _project_result(result: dict, primary_field: str) -> dict:
    fields = (*COMMON_PROJECTION_FIELDS, primary_field)
    return {key: result[key] for key in fields if key in result}
```

- [ ] **Step 2: Define four typed functions**

Inside `create_smartfetch_mcp`, retain the existing `fetch_webpage` signature
and add explicit `webpage_to_markdown` and `extract_webpage_text` functions
with the same signature. Add `render_webpage(url, max_chars=20000)` without a
browser parameter. Each calls `fetch_handler` once; projection tools apply
`_project_result`; render passes `True`.

- [ ] **Step 3: Share wrapper creation while keeping one initialization**

Initialize the resource server and requirements once when enabled. Add a local
helper:

```python
def protect(name, description, handler, extension):
    if not settings.enabled:
        return handler
    wrapper = create_payment_wrapper(
        resource_server,
        accepts=accepts,
        resource=ResourceInfo(
            url=f'mcp://tool/{name}',
            description=description,
            mimeType='application/json',
            serviceName='SmartFetch',
        ),
        extensions=extension,
    )
    return wrapper(handler)
```

Construct each extension from its matching input/output constants. Register
each protected handler with `mcp.tool(name=..., description=...,
structured_output=False)` in `MCP_TOOLS` order.

- [ ] **Step 4: Preserve fail-closed behavior**

Do not alter `_initialize_payment`. Any enabled facilitator, scheme, Bazaar
registration, or wrapper construction exception must prevent app creation.
Do not catch per-tool wrapper errors and do not fall back to an unwrapped tool.

- [ ] **Step 5: Update existing MCP assertions only where behavior expanded**

Change one-tool list/count assertions in `tests/test_v17_mcp.py` and
`tests/test_v18_discovery.py` to the exact four-tool list. Preserve all existing
`fetch_webpage` schema, unpaid challenge, paid verify-once/fetch-once/settle-once,
fail-closed, and no-wallet-secret assertions.

- [ ] **Step 6: Run MCP/Bazaar tests and require GREEN**

Run:

```powershell
python -m unittest tests.test_v17_mcp tests.test_v18_discovery tests.test_v19_discovery -v
```

Expected: all four tools, resources, challenges, projections, and legacy paid
flow pass.

### Task 4: Add pure proxy-aware discovery document builders

**Files:**
- Create: `smartfetch/discovery.py`
- Modify: `tests/test_v19_discovery.py`
- Read: `smartfetch/bazaar.py`

**Interfaces:**
- Produces: `public_base_url(request: Request) -> str`.
- Produces: `public_urls(request: Request) -> dict[str, str]`.
- Produces: `docs_html(urls: Mapping[str, str]) -> str`.
- Produces: `openapi_document(urls: Mapping[str, str]) -> dict`.
- Produces: `llms_text(urls: Mapping[str, str]) -> str`.
- Produces: `robots_text(urls: Mapping[str, str]) -> str`.
- Produces: `sitemap_xml(urls: Mapping[str, str]) -> str`.

- [ ] **Step 1: Add failing pure-builder tests**

Build Starlette `Request` instances with ASGI scopes whose scheme/host are
`https` and `agent.example:9443`. Require:

```python
public_base_url(request) == 'https://agent.example:9443'
public_urls(request) == {
    'base': 'https://agent.example:9443',
    'docs': 'https://agent.example:9443/docs',
    'openapi': 'https://agent.example:9443/openapi.json',
    'llms': 'https://agent.example:9443/llms.txt',
    'robots': 'https://agent.example:9443/robots.txt',
    'sitemap': 'https://agent.example:9443/sitemap.xml',
    'meta': 'https://agent.example:9443/meta',
    'fetch': 'https://agent.example:9443/fetch',
    'mcp': 'https://agent.example:9443/mcp',
}
```

Do not pass raw forwarded headers into these pure tests.

- [ ] **Step 2: Implement URL normalization**

Use only:

```python
base = f'{request.url.scheme}://{request.url.netloc}'
```

Return the fixed path mapping above. Do not import `os`, inspect headers, or
contain the Railway hostname in this module.

- [ ] **Step 3: Test and implement the OpenAPI 3.1 document**

Assert `openapi == '3.1.0'`, V1.9 info, `servers == [{'url': urls['base']}]`,
one `/fetch` POST operation, no other paths, request schema equal to
`FETCH_INPUT_SCHEMA`, success example equal to `FETCH_OUTPUT_EXAMPLE`, and
responses `200`, `400`, `402`, `429`, `502`, `503`, `504`. Validate with:

```python
from fastapi.openapi.models import OpenAPI
OpenAPI.model_validate(document)
```

The 402 schema must describe public x402 challenge fields without a private
key, wallet secret, CDP credential, or fixed payee.

- [ ] **Step 4: Test and implement HTML and llms text**

Require both documents to include all four tool names, `$0.005`, dynamically
generated docs/OpenAPI/MCP URLs where appropriate, `POST /fetch`, and
`https://github.com/Friezaaaa/smartfetch`. Require docs prose to contain
webpage reader, fetch, scrape, extract, Markdown, browser rendering, MCP, and
x402 concepts. Escape every dynamic URL with `html.escape(..., quote=True)` in
HTML.

- [ ] **Step 5: Test and implement robots and sitemap**

Require robots exactly:

```text
User-agent: *
Allow: /
Sitemap: https://agent.example:9443/sitemap.xml
```

plus a final newline. Parse sitemap using `xml.etree.ElementTree`; require the
standard `http://www.sitemaps.org/schemas/sitemap/0.9` namespace and exact URL
set for `/`, `/meta`, `/docs`, `/openapi.json`, `/llms.txt`. Require `/fetch`,
`/mcp`, `/robots.txt`, and `/sitemap.xml` to be absent. Generate XML with
ElementTree so host content is escaped.

- [ ] **Step 6: Scan builders for forbidden infrastructure and secrets**

Assert serialized builder outputs do not contain the Railway hostname,
`X402_PAY_TO`, `CDP_API_KEY`, `private_key`, `wallet_secret`, or an EVM address.

- [ ] **Step 7: Run pure discovery tests and require GREEN**

Run:

```powershell
python -m unittest tests.test_v19_discovery.V19DiscoveryDocumentTests -v
```

Expected: all pure URL/document contracts pass.

### Task 5: Wire free discovery routes and proxy-aware metadata

**Files:**
- Modify: `smartfetch/server.py`
- Modify: `tests/test_v19_discovery.py`
- Modify: `tests/test_v13_api.py`

**Interfaces:**
- Consumes: `public_urls`, `docs_html`, `openapi_document`, `llms_text`, `robots_text`, `sitemap_xml`.
- Preserves: existing metadata fields and `mcp.tool`.
- Produces: five new free GET routes and dynamic discovery metadata.

- [ ] **Step 1: Add failing route integration tests**

With a paid app and `TestClient(base_url='https://agent.example')`, GET every
free route and assert status 200 with no `payment-required` header. Assert exact
content types begin with:

```python
{
    '/docs': 'text/html',
    '/openapi.json': 'application/json',
    '/llms.txt': 'text/plain',
    '/robots.txt': 'text/plain',
    '/sitemap.xml': 'application/xml',
}
```

Assert `/meta` and `/` dynamically report HTTPS links and retain their existing
endpoint, input, payment, and MCP legacy fields.

- [ ] **Step 2: Register free routes before the catch-all**

Import `HTMLResponse` and `PlainTextResponse`. Each route calls
`public_urls(request)` and its pure builder. Return sitemap with
`Response(content=..., media_type='application/xml')`. Keep FastAPI's built-in
docs/OpenAPI disabled and explicitly own `/docs` and `/openapi.json`.

- [ ] **Step 3: Extend metadata compatibly**

Compute URLs per request. Keep:

```python
'mcp': {
    'enabled': True,
    'path': '/mcp',
    'transport': 'streamable-http',
    'tool': 'fetch_webpage',
}
```

and add `url`, `tools` in exact public order. Add `discovery` keys `docs`,
`openapi`, `llms`, `robots`, and `sitemap`. Do not include a fixed deployment
host, payee, facilitator, or credentials.

- [ ] **Step 4: Exercise actual Uvicorn proxy handling**

Extend `UvicornProxyConfigurationTests` using its existing
`create_uvicorn_config(...); config.load(); TestClient(config.loaded_app, ...)`
path. Under `RAILWAY_ENVIRONMENT_ID`, send `Host: test-host.example` and
`X-Forwarded-Proto: https` to `/meta`, `/docs`, `/openapi.json`, `/llms.txt`,
`/robots.txt`, and `/sitemap.xml`. Require every emitted URL to start with
`https://test-host.example/`. Outside Railway with no trust override, require
the framework to keep the untrusted request scheme as HTTP. This test uses
Uvicorn's proxy middleware; application code never parses headers.

- [ ] **Step 5: Prove routes are free and paid endpoints stay paid**

In the paid app, assert all free paths plus MCP initialize/tools/list have no
challenge. Assert `POST /fetch` remains HTTP 402 and all four unpaid MCP tool
calls remain MCP payment challenges.

- [ ] **Step 6: Run server and discovery tests and require GREEN**

Run:

```powershell
python -m unittest tests.test_v13_api tests.test_v19_discovery -v
```

Expected: proxy-aware dynamic links and all free/paid boundaries pass.

### Task 6: Bump V1.9 metadata and update required assertions/docs

**Files:**
- Modify: `smartfetch/config.py`
- Modify: `server.json`
- Modify: `README.md`
- Modify: `tests/test_v13_api.py`
- Modify: `tests/test_v14_payments.py`
- Modify: `tests/test_v15_bazaar.py`
- Modify: `tests/test_v17_mcp.py`
- Modify: `tests/test_v18_discovery.py`
- Modify: `tests/test_v19_discovery.py`
- Modify: `tests/api_local_smoke.py`
- Modify: `tests/mcp_local_smoke.py`

**Interfaces:**
- Produces: `SERVICE_VERSION = '1.9.0'` everywhere public output depends on it.
- Preserves: exact Registry namespace casing and one remote endpoint.

- [ ] **Step 1: Bump the service version and literal assertions**

Set only:

```python
SERVICE_VERSION = '1.9.0'
```

Update existing `1.8.0` response/example assertions to `1.9.0`. Do not weaken
other exact assertions or bulk-rewrite unrelated content.

- [ ] **Step 2: Update and test exact `server.json`**

Require:

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.Friezaaaa/smartfetch",
  "title": "SmartFetch",
  "description": "Read, fetch, scrape, and render public webpages into clean text, Markdown, links, and metadata.",
  "version": "1.9.0",
  "repository": {
    "url": "https://github.com/Friezaaaa/smartfetch",
    "source": "github"
  },
  "remotes": [
    {
      "type": "streamable-http",
      "url": "https://smartfetch-production-ea53.up.railway.app/mcp"
    }
  ]
}
```

Assert one remote, no packages, headers, keys, wallet fields, credentials, or
other URLs.

- [ ] **Step 3: Update README precisely**

Change the title to V1.9 and explain:

- V1.8 was successfully published to the Official MCP Registry;
- V1.9 adds three MCP views without changing `fetch_webpage`;
- all four tool names, arguments, output distinctions, and unique resources;
- all five new free routes and dynamic proxy-aware URL behavior;
- HTTP `/fetch` and MCP calls remain `$0.005` exact x402 executions;
- natural discovery phrases: read a webpage, fetch a webpage, scrape a URL,
  retrieve website content, convert webpage to Markdown, extract clean text,
  read a JavaScript website, browser render a webpage, web scraping for an AI
  agent, get webpage content for an agent, website to Markdown, fetch public
  URL;
- no deployment, registry republish, Bazaar seed call, or payment occurs in
  the repository change.

Remove the stale sentence that Registry publication is still a future
operator action. Preserve the current payment configuration and security
documentation.

- [ ] **Step 4: Update local smoke expectations**

In `tests/api_local_smoke.py`, assert V1.9 and GET all five new free routes.
Parse OpenAPI and sitemap and require their expected top-level fields. In
`tests/mcp_local_smoke.py`, require the exact four tools but keep one unpaid
`fetch_webpage` challenge, amount/network/scheme checks, and
`fetch.assert_not_awaited()`.

- [ ] **Step 5: Run all version/manifest/smoke-related unit tests**

Run:

```powershell
python -m unittest tests.test_v13_api tests.test_v14_payments tests.test_v15_bazaar tests.test_v17_mcp tests.test_v18_discovery tests.test_v19_discovery -v
```

Expected: all exact V1.9 metadata assertions pass with no relaxed V1.8
behavior checks.

### Task 7: Run the complete no-payment verification gate and review scope

**Files:**
- Verify all changed files from Tasks 1-6.
- Verify unchanged: `requirements.txt`, `smartfetch/payments.py`, `smartfetch/core.py`, `smartfetch/extract.py`, `smartfetch/browser_fetch.py`, `smartfetch/http_fetch.py`, `smartfetch/limits.py`, `smartfetch/security.py`, `scripts/paid_fetch_test.py`, `scripts/paid_fetch_mainnet_test.py`.

**Interfaces:**
- Produces: evidence that the uncommitted V1.9 tree is correct, scoped, and ready for user review.

- [ ] **Step 1: Run focused V1.9 tests fresh**

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:X402_ENABLED='false'
python -B -m unittest tests.test_v19_discovery -v
```

Require all focused tests to pass.

- [ ] **Step 2: Run the complete unit suite fresh**

```powershell
python -B -m unittest discover -s tests -p 'test_*.py' -v
```

Require zero failures and zero errors. This suite must not run either paid
smoke script's `main()` and must make no live payment.

- [ ] **Step 3: Run security and local API smokes**

```powershell
python -B tests/security_smoke.py
python -B tests/api_local_smoke.py
```

Require security smoke PASS and local API smoke PASS for health, metadata,
discovery documents, and one localhost fixture retrieval.

- [ ] **Step 4: Run mocked MCP smoke**

```powershell
python -B tests/mcp_local_smoke.py
```

Require free initialize, exact four-tool list, one unpaid x402 challenge, and
no retrieval. This is mocked/local only and signs or settles nothing.

- [ ] **Step 5: Run the local retrieval 20/20 gate**

```powershell
python -B tests/local_20.py
```

Require 20/20. Confirm `tests/local_20_results.json` remains ignored/untracked
and produces no real Git diff; do not alter test source or expected fixtures.

- [ ] **Step 6: Validate the Registry manifest if publisher is installed**

First run:

```powershell
Get-Command mcp-publisher -ErrorAction SilentlyContinue
```

If found, run:

```powershell
mcp-publisher validate server.json
```

Require validation success. If absent, report the command as unavailable; do
not install or publish anything.

- [ ] **Step 7: Verify formatting and protected scope**

Run:

```powershell
git diff --check
git diff --name-status
git status --short
git diff -- requirements.txt smartfetch/payments.py smartfetch/core.py smartfetch/extract.py smartfetch/browser_fetch.py smartfetch/http_fetch.py smartfetch/limits.py smartfetch/security.py scripts/paid_fetch_test.py scripts/paid_fetch_mainnet_test.py
```

Require zero diff for every protected file and no generated artifacts. Search
changed runtime files for the Railway hostname and require it to appear only in
`server.json`:

```powershell
rg -n "smartfetch-production-ea53\.up\.railway\.app" smartfetch README.md server.json
```

- [ ] **Step 8: Present the uncommitted review package and stop**

Show the complete uncommitted diff, exact changed-file list, dependency status
(`requirements.txt` unchanged), every command/result from the gate,
`mcp-publisher` validation outcome, and final `git status --short`. Do not
commit, push, deploy, publish, seed Bazaar, or make a payment.
