# SmartFetch V1.9 Phase 1 Discovery Design

## Context

SmartFetch V1.8 exposes one paid HTTP operation (`POST /fetch`) and one paid
MCP tool (`fetch_webpage`). The HTTP resource and MCP tool both reuse the same
retrieval engine, x402 configuration, and Bazaar schema. V1.9 expands the
number of machine-friendly ways agents and crawlers can discover those
capabilities without changing retrieval, payment, or deployment behavior.

This design has two independently testable surfaces:

1. Three additional paid MCP tool views over the existing retrieval result.
2. Free, proxy-aware web discovery documents for humans, crawlers, and agents.

## Goals

- Expose exactly four MCP tools: `fetch_webpage`, `webpage_to_markdown`,
  `extract_webpage_text`, and `render_webpage`.
- Keep all four tools behind the existing x402 exact payment flow at `$0.005`,
  using the configured network, payee, and facilitator.
- Give every tool a unique MCP payment resource and accurate Bazaar metadata.
- Add `/docs`, `/openapi.json`, `/llms.txt`, `/robots.txt`, and `/sitemap.xml`
  as free discovery routes.
- Generate every runtime public URL from FastAPI/Starlette's proxy-aware
  request scheme and host, normalized to `scheme://host`.
- Bump runtime and registry metadata to `1.9.0`.

## Non-goals and invariants

- Do not change `smartfetch/core.py`, extraction, browser fallback, SSRF
  checks, concurrency, timeouts, output limits, or retry behavior.
- Do not change `POST /fetch`, the HTTP Bazaar resource identity, x402 2.20.0,
  the `exact` scheme, `$0.005` default price, Base Sepolia, Base mainnet, CDP
  facilitator behavior, or `X402_PAY_TO` handling.
- Do not call MCP tools through the public HTTP endpoint.
- Do not add a seller private key, wallet secret, payment credential, header,
  or package.
- Do not change either paid-client smoke script.
- Do not publish, deploy, seed Bazaar, make a payment, commit, or push during
  this implementation phase.
- The fixed Railway `/mcp` URL remains only in root `server.json` for the
  Official MCP Registry. Runtime discovery responses never hard-code it.

## Architectural decision

Use a hybrid design: keep four explicit typed MCP functions so FastMCP emits
stable, readable tool schemas, while sharing payment wrapping, result
projection, and Bazaar construction through small helpers. Fully duplicating
four wrapper blocks would invite price/resource/schema drift. Dynamically
generating the tool functions would hide their signatures from FastMCP and
make schema correctness harder to audit.

Free discovery content belongs in a new `smartfetch/discovery.py` module.
This keeps `smartfetch/server.py` focused on route wiring and lets focused
tests validate URL normalization and document bodies without starting a
server.

## MCP tool contracts

All tools execute the existing `FetchHandler` internally. Payment wrapping is
installed before registration, so an unpaid tool call never reaches the
handler. MCP `initialize` and `tools/list` remain unwrapped and free.

### `fetch_webpage`

- Description remains exactly `FETCH_DESCRIPTION`.
- Inputs remain `url`, `max_chars=20000` (minimum 1000, maximum 50000), and
  `force_browser=false`.
- Returns the complete existing SmartFetch result unchanged.
- Payment resource remains `mcp://tool/fetch_webpage`.

### `webpage_to_markdown`

- Description: `Convert a public webpage or URL into clean Markdown for AI
  agents, with core retrieval metadata.`
- Inputs are `url`, `max_chars=20000` (minimum 1000, maximum 50000), and
  `force_browser=false`.
- Calls the same handler once and returns `markdown` plus the common result
  fields listed below. It must not return the full `content` field.
- Payment resource is `mcp://tool/webpage_to_markdown`.

### `extract_webpage_text`

- Description: `Extract clean readable text from a public webpage or URL for
  AI agents, with core retrieval metadata.`
- Inputs are `url`, `max_chars=20000` (minimum 1000, maximum 50000), and
  `force_browser=false`.
- Calls the same handler once and returns `content` plus the common result
  fields listed below. It must not return the `markdown` field.
- Payment resource is `mcp://tool/extract_webpage_text`.

### `render_webpage`

- Description: `Browser-render a public JavaScript-heavy webpage or URL, then
  return clean text, Markdown, links, and metadata.`
- Inputs are only `url` and `max_chars=20000` (minimum 1000, maximum 50000).
- Calls the same handler once with `force_browser=True` and returns the complete
  existing SmartFetch result unchanged.
- Payment resource is `mcp://tool/render_webpage`.

### Projection fields

The Markdown and text projections copy only these common fields when present:

- `success`
- `requested_url`
- `final_url`
- `status_code`
- `render_method`
- `elapsed_ms`
- `fallback_reason`
- `title`
- `truncated`
- `max_chars`
- `request_id`
- `service_version`

`webpage_to_markdown` adds `markdown`; `extract_webpage_text` adds `content`.
The projection is applied after successful internal retrieval. Exceptions
continue through the existing MCP/x402 wrapper behavior.

## Shared x402 and Bazaar wiring

`create_smartfetch_mcp` initializes one existing x402 resource server and one
set of payment requirements. When payments are enabled, a helper wraps each
tool with:

- the same `resource_server`;
- the same `accepts` built from `settings.price`, `settings.pay_to`, and
  `settings.network`;
- a per-tool `ResourceInfo` using `mcp://tool/<tool-name>`;
- a per-tool official MCP Bazaar extension.

`smartfetch/payments.py` does not change. It already registers
`bazaar_resource_server_extension` through
`create_x402_resource_server(..., register_bazaar=True)`.

`smartfetch/bazaar.py` gains one generalized MCP discovery builder accepting:

```python
def mcp_discovery_extension(
    *,
    tool_name: str,
    description: str,
    input_schema: dict,
    input_example: dict,
    output_schema: dict,
    output_example: dict,
    transport: str = "streamable-http",
) -> dict:
```

It calls the installed x402 2.20.0
`declare_mcp_discovery_extension(DeclareMcpDiscoveryConfig(...))`. The existing
`fetch_mcp_discovery_extension()` remains as a compatibility wrapper using the
unchanged full fetch schemas. Per-tool input/output constants reuse shared URL,
character-limit, metadata, and example fragments rather than copying
equivalent structures.

The HTTP `fetch_discovery_extension()` remains semantically unchanged and
continues to advertise type `http`, method `POST`, and endpoint `/fetch`.

## Proxy-aware public URL construction

`smartfetch.discovery.public_base_url(request)` reads only Starlette's parsed
`request.url.scheme` and `request.url.netloc` and returns:

```text
scheme://host
```

with no trailing slash. It does not read `Forwarded`, `X-Forwarded-*`, or
`Host` headers directly. Existing Uvicorn configuration remains responsible
for deciding which proxy headers are trusted. On Railway,
`create_uvicorn_config` continues to trust Railway's proxy; outside Railway it
retains Uvicorn's safer default unless `FORWARDED_ALLOW_IPS` is explicitly set.

All runtime discovery links are formed by appending fixed absolute paths to
this base. This includes links embedded in `/docs`, `/openapi.json`,
`/llms.txt`, `/robots.txt`, `/sitemap.xml`, and `/meta`.

## Free discovery route contracts

All routes are registered before the catch-all route and remain outside x402
protection.

### `GET /docs`

Returns UTF-8 `text/html`. It describes SmartFetch as a webpage reader,
fetcher, scraper, text extractor, Markdown converter, browser renderer, remote
MCP server, and x402-paid service. It lists the free and paid endpoints, the
four MCP tools, `$0.005` price, safe request examples, a dynamic OpenAPI link,
a dynamic MCP endpoint link, and the public GitHub repository. It contains no
credentials, wallet destination, facilitator configuration, or internal host.

### `GET /openapi.json`

Returns a valid OpenAPI 3.1 document for the existing `POST /fetch` contract.
It includes:

- `openapi: 3.1.0`;
- service name, description, and version `1.9.0`;
- one server URL equal to the normalized public base URL;
- one `/fetch` `post` operation;
- the existing request schema and representative success example;
- documented `200`, `400`, `402`, `429`, `502`, `503`, and `504` responses;
- a generic x402 v2 payment-required response schema that exposes no secret;
- a dynamic `externalDocs.url` pointing to `/docs`.

The document is descriptive only. FastAPI automatic docs and schema generation
remain disabled so the public contract cannot drift through unrelated route
introspection.

### `GET /llms.txt`

Returns UTF-8 `text/plain`. It lists capabilities, dynamic docs/OpenAPI/MCP
URLs, `POST /fetch`, all four MCP tools, the `$0.005` per-execution price, and
the GitHub repository. It does not embed the active payee or credentials.

### `GET /robots.txt`

Returns UTF-8 `text/plain` with `User-agent: *`, `Allow: /`, and a dynamic
absolute `Sitemap: <base>/sitemap.xml` line.

### `GET /sitemap.xml`

Returns valid UTF-8 `application/xml` using the standard sitemap namespace.
It lists these free public discovery URLs:

- `/`
- `/meta`
- `/docs`
- `/openapi.json`
- `/llms.txt`

It deliberately excludes paid execution endpoints `/fetch` and `/mcp`, and
does not list `/robots.txt` or the sitemap itself as content pages.

## `/meta` compatibility and additions

Existing fields remain. The `mcp` object retains `enabled`, `path`,
`transport`, and legacy `tool: fetch_webpage`, and adds:

```json
{
  "url": "<public-base>/mcp",
  "tools": [
    "fetch_webpage",
    "webpage_to_markdown",
    "extract_webpage_text",
    "render_webpage"
  ]
}
```

A new `discovery` object contains dynamic absolute URLs for `docs`, `openapi`,
`llms`, `robots`, and `sitemap`. The root `/` continues to return the same
metadata response as `/meta`.

## Registry and documentation metadata

- `SERVICE_VERSION` becomes `1.9.0`.
- Root `server.json` keeps the exact name
  `io.github.Friezaaaa/smartfetch`, repository, and one fixed Railway
  Streamable HTTP remote. Its version becomes `1.9.0` and description becomes
  `Read, fetch, scrape, and render public webpages into clean text, Markdown,
  links, and metadata.`
- `README.md` records that V1.8 was successfully published to the Official MCP
  Registry, documents V1.9's four tools and free routes, includes natural
  search phrases, and removes wording that says Registry publication has not
  occurred.
- No dependency version changes are required.

## Tests

Focused V1.9 tests will verify:

- `tools/list` returns exactly the four names in the defined order;
- each tool description and JSON input schema is exact and distinct where
  required;
- the original `fetch_webpage` schema and full output behavior are unchanged;
- each paid wrapper receives the shared requirements, its unique resource,
  and its matching Bazaar extension;
- every unpaid tool call returns a valid x402 challenge before retrieval;
- projection tools omit the unwanted full representation;
- `render_webpage` invokes the handler once with `force_browser=True`;
- payment price, atomic amount, exact scheme, network, payee, and facilitator
  remain sourced from existing settings;
- the HTTP Bazaar record remains type `http`, method `POST`, and `/fetch`;
- all free discovery routes and MCP initialize/list remain unchallenged;
- proxy-aware HTTPS and host values flow through every dynamic discovery URL;
- OpenAPI validates as 3.1 JSON; HTML, text, robots, and sitemap bodies contain
  the intended public data and no secrets;
- `server.json` has the exact V1.9 remote-only shape and no packages or secret
  fields.

Existing V1.3-V1.8 assertions that necessarily pin the service version or one
tool count will be updated. Existing behavior assertions remain intact.

## Validation gate

Run, without any live payment or deployment:

1. Focused V1.9 tests.
2. Complete unit suite.
3. Security smoke.
4. Local API smoke.
5. Local mocked-payment MCP smoke.
6. Local retrieval 20/20 gate.
7. `mcp-publisher validate server.json` when the installed publisher is
   available.
8. `git diff --check`.
9. Protected-file hash/diff verification and `git status --short`.

## Risks and mitigations

- **Proxy/host correctness:** URL generation relies only on the framework URL,
  so it inherits the existing explicit Uvicorn trust boundary. An actual
  Uvicorn forwarded-HTTPS regression test covers this path.
- **Schema drift:** shared property/example fragments and explicit focused
  equality tests keep MCP Bazaar, MCP tool schemas, and OpenAPI aligned.
- **Payment bypass:** wrappers are applied before tool registration; unpaid
  calls are tested across all four tools with a retrieval mock that must remain
  untouched.
- **Double charging:** handlers call the internal retrieval callable, never
  `/fetch`; paid execution tests assert one retrieval and one settlement.
- **Unexpected output bloat:** Markdown and text projections use allowlisted
  fields and explicitly exclude the alternate full representation.
- **Route shadowing:** discovery routes and `/mcp` are registered before the
  catch-all and are exercised by integration tests.
- **Crawler injection:** HTML/XML output escapes dynamic URLs. No raw forwarded
  header value is parsed or interpolated.
