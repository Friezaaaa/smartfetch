# SmartFetch V1.11 Search, Structured Extraction, and Multimodal Design

Status: draft architecture pending final approval; production implementation
has not begun.

Baseline: SmartFetch V1.10.6 at
`afc7a6466cdbc86f3b9c5921a67ae6bcf3180824`.

## 1. Context and invariants

SmartFetch V1.10.6 exposes one paid HTTP retrieval route and four paid MCP
tools. It uses x402 v2 `exact` payments on Base Sepolia or Base mainnet,
verifies authorization before execution, and settles only after a successful
response. The existing retrieval engine tries ordinary HTTP first and uses
Chromium when explicitly requested or when the HTTP result is inadequate. It
also supplies SSRF validation, redirect validation, bounded response handling,
timeouts, concurrency controls, privacy-safe activity logging, OpenAPI,
`/.well-known/x402`, Bazaar metadata, and an Official MCP Registry manifest.

V1.11 adds search and schema-valid multimodal extraction without changing the
four current tools or `POST /fetch`. The production release will expose exactly
six MCP tools:

1. `fetch_webpage` (unchanged, `$0.005`)
2. `webpage_to_markdown` (unchanged, `$0.005`)
3. `extract_webpage_text` (unchanged, `$0.005`)
4. `render_webpage` (unchanged, `$0.005`)
5. `search_and_extract` (new, variant-priced)
6. `extract_structured_data` (new, variant-priced)

The following are release invariants:

- Existing HTTP and MCP contracts, prices, resource identities, payment
  configuration, facilitator selection, settlement semantics, fallback order,
  SSRF behavior, timeouts, and limits remain compatible.
- Existing tools continue to initialize and execute when `GEMINI_API_KEY` and
  `EXA_API_KEY` are absent.
- MCP initialize and `tools/list`, plus all current discovery routes, remain
  free.
- Only the eight finite new variants described below exist. No wildcard route
  or open-ended paid variant is registered.
- No provider, download, retrieval, or model work starts until the applicable
  x402 authorization has been verified.
- No failed, partial, invalid, or status-400-or-higher result settles.
  Schema-permitted nulls disclosed under section 6.3 are not partial or invalid
  when usable non-null requested data remains and all other checks succeed.
- No payment retry, provider retry, or automatic paid retry is added.
- Provider credentials, payment proofs, schemas, prompts, URLs, source content,
  media, and model output never enter activity logs.
- No login credentials, cookies, caller authorization headers, raw uploads,
  file paths, private URLs, CAPTCHA bypass, or access-control circumvention are
  accepted.

## 2. Current architecture findings

### 2.1 Retrieval and security

`smartfetch.core.smart_fetch()` is the single retrieval engine. It preserves
these behaviors:

- validates the initial target and every redirect through
  `validate_public_url()`;
- blocks credentials, unsupported schemes and ports, loopback/private/link-
  local/reserved targets, and DNS results that are not globally routable;
- streams HTTP bodies into a bounded byte buffer;
- performs the existing one retry for transient HTTP/network failures;
- falls back to Chromium after an inadequate HTTP result;
- limits output text, Markdown, links, request body size, total duration, and
  HTTP/browser concurrency;
- emits typed, privacy-safe retrieval diagnostics on final failures.

V1.11 must call this engine for webpages and search-selected webpages. It must
not create another scraper. The new webpage `render_mode` maps to the existing
engine as follows:

- `auto` (default): current HTTP-first behavior with existing browser fallback;
- `always`: current `force_browser=True` behavior.

V1.11 does not add an HTTP-only mode and does not modify
`smartfetch.core.smart_fetch()`. `auto` and `always` map directly to behavior
the engine already supports. Duplicating the HTTP/extraction orchestration in a
new V1.11 module is rejected.

### 2.2 Current payment lifecycle

The HTTP service registers a static `RouteConfig` for `POST /fetch`. The x402
middleware verifies the signed authorization before the FastAPI handler runs;
it settles only after the handler returns a successful application result.
Existing tests establish that status 400 or greater does not settle.

Each MCP tool is wrapped with x402 2.20.0's official
`create_payment_wrapper()`. The wrapper verifies before calling the tool,
returns an MCP error result without settlement when tool execution fails, and
settles after a successful tool result. Initialize and `tools/list` bypass the
tool wrappers and remain free.

V1.11 retains those lifecycle rules. It does not claim that signing itself is
reversible: a buyer can sign an authorization that verifies, then receive an
execution failure. In that case SmartFetch does not settle the authorization
and no USDC transfer should occur. "Successful delivery" means the application
has produced a contract-valid success result. The official wrappers settle
before that result is returned through the transport, so neither HTTP nor MCP
can prove that the client received every response byte before settlement. The
documentation and tests must state this transport-level limitation explicitly.

For structured extraction, settlement eligibility includes the missing-value
rules in section 6.3. Properly disclosed schema-permitted nulls can be part of
a contract-valid success; an all-missing/null extraction cannot settle.

### 2.3 Dynamic-pricing feasibility

The installed x402 Python 2.20.0 HTTP API permits `DynamicPrice`, but its
callable receives an `HTTPRequestContext`. Although the adapter protocol has a
`get_body()` member, the installed FastAPI adapter returns `None` because body
access is asynchronous. Consequently, a price cannot safely depend on a JSON
body field such as `mode` or `source_type` in the current middleware.

The public x402 2.20.0 MCP `create_payment_wrapper()` accepts a static
`list[PaymentRequirements]`; it has no argument-aware price callback. Passing
all prices in one `accepts` list would allow the buyer to choose a cheaper
requirement independently of the requested operation and is therefore unsafe.

The approved solution is:

- concrete, path-discriminated REST resources with one static payment
  requirement each; and
- one user-visible MCP tool per capability, with local finite validation and
  dispatch to a prebuilt static wrapper for the selected variant.

No request-body pricing, argument-dependent mutation of a shared requirement,
custom x402 protocol implementation, or common fixed price is used.

## 3. Considered approaches

### Approach A — finite REST resources and static internal MCP wrappers

This is the approved and recommended approach.

- Register eight exact REST paths, each with a static x402 `RouteConfig`.
- Register exactly two MCP tools. Each tool validates its enum locally and
  calls one of eight prebuilt x402 wrappers.
- Give every internal MCP variant a distinct resource URI while retaining the
  same public MCP tool name.
- Reuse the existing resource server, facilitator, network, asset, and payee.

Advantages: the challenge is unambiguous before signing; cheaper authorization
cannot unlock a more expensive operation; discovery can show each exact price;
the official wrappers preserve current verification and settlement behavior.

Tradeoff: HTTP clients use a path variant instead of putting the discriminator
only in the body. MCP discovery must explain that one tool has a finite set of
priced variants.

### Approach B — one price per user-visible capability

One REST route and one wrapper per MCP tool would be simpler, but it would
charge inexpensive search and costly video processing alike. It contradicts
the approved prices and could neither protect margin nor present a fair buyer
contract. This approach is rejected unless separately approved after a cost
benchmark.

### Approach C — custom argument-aware payment middleware

A custom wrapper could parse HTTP bodies and MCP arguments before generating a
challenge. That would alter the payment architecture, duplicate official x402
logic, and create new verification, replay, error-handling, and settlement
risks. It is rejected for V1.11.

Splitting all variants into separate MCP tools was also considered and rejected
because it would expose more than the two approved new tools.

## 4. Public resources and prices

Only these concrete REST resources are registered:

| Method and path | Provisional price | Required provider(s) |
|---|---:|---|
| `POST /search-and-extract/results` | `$0.05` | Exa |
| `POST /search-and-extract/answer` | `$0.10` | Exa + SmartFetch retrieval + Gemini |
| `POST /search-and-extract/structured` | `$0.15` | Exa + SmartFetch retrieval + Gemini |
| `POST /extract-structured-data/webpage` | `$0.05` | Gemini |
| `POST /extract-structured-data/image` | `$0.05` | Gemini |
| `POST /extract-structured-data/pdf` | `$0.05` | Gemini |
| `POST /extract-structured-data/audio` | `$0.10` | Gemini |
| `POST /extract-structured-data/video` | `$0.15` | Gemini |

Unknown suffixes are not x402 routes. They return an ordinary free 404 (or a
bounded 405 where FastAPI normally does so) before payment, provider, download,
retrieval, model, activity-execution, or settlement work.

The prices above are approved for the design and test fixtures, not production
deployment authorization. A later benchmark must validate the margin rule in
section 13. If a price fails that gate, changing it requires user approval
before deployment.

The internal MCP resources are:

- `mcp://tool/search_and_extract/results`
- `mcp://tool/search_and_extract/answer`
- `mcp://tool/search_and_extract/structured`
- `mcp://tool/extract_structured_data/webpage`
- `mcp://tool/extract_structured_data/image`
- `mcp://tool/extract_structured_data/pdf`
- `mcp://tool/extract_structured_data/audio`
- `mcp://tool/extract_structured_data/video`

The distinct identities make payment and Bazaar records variant-specific. The
MCP `toolName` remains exactly `search_and_extract` or
`extract_structured_data`.

Every requirement is built from the existing x402 resource server with:

- `scheme: exact`;
- active `X402_NETWORK` (`eip155:84532` or `eip155:8453`);
- active configured USDC asset produced by the registered EVM exact scheme;
- active `X402_PAY_TO`;
- only the variant's static price.

No asset contract, payee, or network is duplicated in a V1.11 provider module.

## 5. Exact request contracts

All request objects reject unknown properties. String values are valid UTF-8,
trimmed for validation, and never evaluated as code or templates.

### 5.1 Search REST requests

The path selects the mode; a REST body must not contain `mode`.

`POST /search-and-extract/results`:

```json
{
  "query": "current official x402 Python MCP documentation",
  "max_results": 5,
  "domains": ["docs.x402.org"],
  "freshness": "month"
}
```

`POST /search-and-extract/answer`:

```json
{
  "query": "What changed recently in the x402 Python MCP API?",
  "max_results": 5,
  "domains": ["docs.x402.org"],
  "freshness": "month"
}
```

`POST /search-and-extract/structured`:

```json
{
  "query": "current x402 Python MCP release details",
  "max_results": 5,
  "max_sources": 3,
  "domains": ["docs.x402.org"],
  "freshness": "month",
  "json_schema": {
    "type": "object",
    "properties": {
      "version": {"type": "string"},
      "release_date": {"type": ["string", "null"]}
    },
    "required": ["version", "release_date"],
    "additionalProperties": false
  },
  "instructions": "Use the current stable Python release."
}
```

Field constraints:

| Field | Constraint |
|---|---|
| `query` | required string, 1–500 characters after trimming |
| `max_results` | Exa candidate/result count; integer, default 5, minimum 1, maximum 10 in every mode |
| `max_sources` | `structured` only; number of top Exa candidates SmartFetch retrieves, integer 1–3, default 3, and no greater than `max_results` |
| `domains` | optional include-only list, 1–10 unique normalized public DNS hostnames; no scheme, credentials, port, path, query, fragment, wildcard, or IP literal |
| `freshness` | optional enum: `day`, `week`, `month`, `year` |
| `json_schema` | required only for `structured`; forbidden for `results` and `answer` |
| `instructions` | optional only for `structured`; 1–2,000 characters |

`freshness` is converted server-side to an absolute published-after timestamp
once per request and passed to Exa. It is returned in public metadata so the
request remains auditable. Results mode returns up to `max_results` entries.
Answer mode asks Exa for up to `max_results` candidates and SmartFetch retrieves
the top `min(max_results, 3)` candidates. Structured mode retrieves exactly up
to `max_sources` from at most `max_results` candidates. The candidate and
retrieval bounds are explicit and never silently conflated.

### 5.2 Search MCP request

The single `search_and_extract` input schema is:

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "minLength": 1, "maxLength": 500},
    "mode": {"type": "string", "enum": ["results", "answer", "structured"]},
    "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
    "max_sources": {"type": "integer", "minimum": 1, "maximum": 3},
    "domains": {
      "type": "array",
      "maxItems": 10,
      "uniqueItems": true,
      "items": {"type": "string"}
    },
    "freshness": {"type": "string", "enum": ["day", "week", "month", "year"]},
    "json_schema": {"type": "object"},
    "instructions": {"type": "string", "minLength": 1, "maxLength": 2000}
  },
  "required": ["query", "mode"],
  "additionalProperties": false
}
```

Cross-field rules are enforced before selecting a paid wrapper. `max_sources`
is accepted only for structured mode, defaults internally to 3 when omitted
there, and cannot exceed `max_results`.
An invalid mode, a missing structured schema, a schema supplied to another
mode, or an invalid count combination returns a free MCP input error and cannot
produce a challenge.

### 5.3 Structured-source REST requests

The path selects the source type; a REST body must not contain `source_type`.

Webpage example:

```json
{
  "source_url": "https://example.com/",
  "render_mode": "auto",
  "json_schema": {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": false
  },
  "instructions": "Extract the page title."
}
```

Image, PDF, audio, and video use the same object without `render_mode`:

```json
{
  "source_url": "https://cdn.example.org/public/report.pdf",
  "json_schema": {
    "type": "object",
    "properties": {"total": {"type": ["number", "null"]}},
    "required": ["total"],
    "additionalProperties": false
  },
  "instructions": "Extract the reported total."
}
```

Field constraints:

| Field | Constraint |
|---|---|
| `source_url` | required public HTTPS URL, maximum 4,096 characters; no credentials or nonstandard port |
| `render_mode` | webpage only; enum `auto`, `always`; default `auto`; forbidden on other source types |
| `json_schema` | required bounded schema described in section 8 |
| `instructions` | optional string, 1–2,000 characters |

### 5.4 Structured-source MCP request

The single `extract_structured_data` tool has the same fields plus required
`source_type` with enum `webpage`, `image`, `pdf`, `audio`, or `video`.
Cross-field rules and provider readiness are evaluated before selecting the
static wrapper. Invalid source types and invalid `render_mode` combinations
produce no challenge.

## 6. Exact success response contracts

Every success response includes:

```json
{
  "success": true,
  "request_id": "a1b2c3d4e5f60708",
  "service_version": "1.11.0",
  "retrieved_at": "2026-09-02T15:04:05Z"
}
```

Timestamps are server-generated UTC RFC 3339 values. Provider-specific
response objects and internal cost records never enter the public result.

### 6.1 `results`

```json
{
  "success": true,
  "request_id": "a1b2c3d4e5f60708",
  "service_version": "1.11.0",
  "mode": "results",
  "query": "current official x402 Python MCP documentation",
  "freshness_after": "2026-08-02T15:04:05Z",
  "retrieved_at": "2026-09-02T15:04:05Z",
  "results": [
    {
      "source_id": "s1",
      "rank": 1,
      "title": "MCP Server with x402",
      "url": "https://docs.x402.org/guides/mcp-server-with-x402",
      "snippet": "Official guidance for protecting MCP tools with x402.",
      "published_at": null
    }
  ]
}
```

At most `max_results` entries are returned. Titles are capped at 300
characters, public URLs at 4,096, snippets at 800, and publication dates are
RFC 3339 or `null`.

### 6.2 `answer`

```json
{
  "success": true,
  "request_id": "a1b2c3d4e5f60708",
  "service_version": "1.11.0",
  "mode": "answer",
  "query": "What changed recently in the x402 Python MCP API?",
  "freshness_after": "2026-08-02T15:04:05Z",
  "retrieved_at": "2026-09-02T15:04:05Z",
  "answer": "The current official wrapper supports paid MCP tool execution.",
  "claims": [
    {"text": "The MCP wrapper protects tool execution.", "citation_ids": ["c1"]}
  ],
  "citations": [
    {
      "citation_id": "c1",
      "title": "MCP Server with x402",
      "url": "https://docs.x402.org/guides/mcp-server-with-x402",
      "published_at": null
    }
  ]
}
```

The answer is capped at 12,000 characters, claims at 50, claim text at 500,
citations at 20, and every claim must reference at least one returned citation.
Exa selects up to `max_results` candidates and SmartFetch retrieves at most the
top three through its existing safe pipeline before Gemini synthesis. Gemini
receives opaque source IDs and may return only those IDs in `citation_ids`; it never
supplies a citation URL. SmartFetch builds public citation titles, URLs, and
dates from the Exa-selected, successfully retrieved source registry. An
unknown source ID, an uncited claim, or a citation to an unretrieved source is
a delivery failure and does not settle.

### 6.3 `structured`

```json
{
  "success": true,
  "request_id": "a1b2c3d4e5f60708",
  "service_version": "1.11.0",
  "mode": "structured",
  "retrieved_at": "2026-09-02T15:04:05Z",
  "data": {"version": "2.20.0", "release_date": null},
  "sources": [
    {
      "source_id": "s1",
      "title": "MCP Server with x402",
      "url": "https://docs.x402.org/guides/mcp-server-with-x402",
      "retrieval_method": "http",
      "retrieved_at": "2026-09-02T15:04:04Z"
    }
  ],
  "evidence": [
    {
      "field": "/version",
      "source_id": "s1",
      "quote": "x402 Python 2.20.0"
    }
  ],
  "missing_fields": ["/release_date"],
  "uncertainties": [
    {"field": "/release_date", "reason": "No release date was present in the retrieved sources."}
  ]
}
```

The example above is settlement eligible under the section 5.1 schema:
`version` is usable non-null data supported by evidence, while `release_date`
explicitly permits null and has both a `missing_fields` pointer and a bounded
`uncertainties` entry. No positive evidence for the absent date is required.

The same envelope is returned by `extract_structured_data`, with
`source_type` and `retrieval_method` added. `retrieval_method` is one of
`http`, `browser`, `image`, `pdf`, `audio`, or `video`.

Limits:

- serialized `data`: at most 64 KiB;
- sources: at most 3 for structured search and exactly 1 for direct extraction;
- evidence: at most 100 entries;
- `field`: RFC 6901 JSON Pointer, at most 256 characters;
- `quote`: optional, at most 500 characters;
- visual `description`: optional, at most 300 characters;
- `page`: optional bounded positive integer;
- `start_seconds`/`end_seconds`: optional non-negative finite numbers inside
  the inspected duration;
- missing fields: at most 64 JSON pointers;
- uncertainties: at most 64 entries, each reason at most 300 characters.

For textual webpage and PDF evidence, a normalized quote must occur in the
bounded source text supplied to the model. Image evidence uses a bounded visual
description. Audio/video evidence uses inspected time ranges. Evidence must
refer to a returned source and a real field in `data`. Every non-null present
required leaf field, including required leaves nested in objects and arrays,
must have at least one valid bounded evidence entry whose `field` is its
canonical RFC 6901 JSON Pointer. Array indices are explicit pointer segments.
The 100-entry evidence cap bounds the number of evidenced non-null required
leaves a request can deliver.

A required leaf may be null only when the caller's schema explicitly permits
null. Every allowed null must be present in `data`, listed by canonical pointer
in `missing_fields`, and have a corresponding bounded `uncertainties` entry
explaining its absence. Allowed nulls are exempt from positive quote, visual,
or timestamp evidence: absence cannot be proven by a positive value citation.
These disclosures remain subject to the existing 64-entry bounds.

An allowed, properly disclosed null does not make the result partial or
invalid. It may settle if usable non-null requested data remains and the rest
of the contract succeeds. A missing required value whose schema does not
permit null fails validation. If all requested values are missing/null and
there is no usable non-null requested data, extraction fails with the existing
finite `evidence_validation_failed` contract (HTTP 422 or MCP error result).
Neither failure settles. Metadata, source records, evidence, and uncertainty
text do not count as usable requested data.

Omitted required values, undisclosed nulls, unevidenced non-null required
leaves, schema-invalid values, and contradictory values remain failed/partial
results and must not settle. Optional omissions remain governed by the caller
schema; they cannot bypass the usable-data requirement. These rules apply to
structured search and every direct structured-extraction source type.

## 7. Failure contract and status mapping

REST failures retain one bounded shape:

```json
{
  "success": false,
  "error_code": "schema_validation_failed",
  "error": "The extracted result did not satisfy the requested schema.",
  "request_id": "a1b2c3d4e5f60708",
  "service_version": "1.11.0"
}
```

MCP returns the same object as an error `CallToolResult`. Neither transport
returns raw provider exceptions. Stable codes and HTTP statuses are:

| Status | Codes |
|---:|---|
| 400 | `invalid_request`, `invalid_schema`, `invalid_filter`, `invalid_source_url` |
| 402 | official x402 payment challenge |
| 413 | `source_too_large`, `schema_too_large` |
| 415 | `unsupported_media_type` |
| 422 | `invalid_provider_output`, `schema_validation_failed`, `evidence_validation_failed` |
| 502 | `search_failed`, `retrieval_failed`, `model_failed`, `provider_cleanup_failed` |
| 503 | `provider_unavailable`, `capacity_unavailable` |
| 504 | `provider_timeout`, `retrieval_timeout` |

Unknown REST variants remain framework 404/405 responses and never reach the
paid capability layer. Unknown MCP enum values are MCP input errors.

## 8. Caller schema safety

V1.11 does not claim arbitrary JSON Schema support. It accepts a deliberately
small subset that both Gemini structured output and local validation can
enforce deterministically.

Limits are applied before selecting a paid wrapper where the transport permits
local argument validation, and always before provider or retrieval work:

- canonical UTF-8 JSON representation at most 16 KiB;
- root must be an object schema with `type: "object"`;
- maximum nesting depth 6;
- maximum 64 object properties across the entire schema;
- array nesting is included in the overall depth-6 limit;
- `required` contains at most 64 unique declared property names;
- every array must declare `items`; optional `maxItems` cannot exceed 100;
- total enum values at most 128; each serialized enum value at most 256 bytes;
- property names at most 128 characters; descriptions at most 500 characters;
- `additionalProperties` must be `false` on every object;
- maximum expected output size 64 KiB.

Allowed keywords are only:

- `$schema`, `title`, `description`;
- `type` using `object`, `array`, `string`, `number`, `integer`, `boolean`, or
  `null`, including a two-element nullable type array;
- `properties`, `required`, `additionalProperties`;
- `items`, `minItems`, `maxItems`;
- `enum`, `const`;
- `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`;
- `minLength`, `maxLength`.

`$ref`, recursive schemas, definitions, `allOf`, `anyOf`, `oneOf`, `not`,
conditionals, regex patterns, `patternProperties`, arbitrary formats,
dependencies, unevaluated keywords, remote references, and custom keywords are
rejected. Validation walks plain parsed data; it performs no dynamic imports,
class generation, code execution, template rendering, expression evaluation,
or network resolution. A pinned `jsonschema` validator performs the final local
validation after the subset guard and after JSON parsing.

Webpage text, media, search results, instructions, and schemas are untrusted
data. Provider prompts delimit them as data and explicitly prohibit following
embedded instructions. Gemini tools are disabled for every V1.11 mode: built-in
web search, code execution, URL context, file search, function calls, computer
use, and Maps are not enabled. Exa selects every search source, and SmartFetch
validates and retrieves every source used by Gemini.

## 9. Provider and media architecture

### 9.1 Provider interfaces

Small typed adapters isolate provider SDK objects:

- `SearchProvider.search(request) -> SearchProviderResult`
- `ModelProvider.synthesize_answer(request) -> CitedAnswerResult`
- `ModelProvider.extract_text(request) -> StructuredModelResult`
- `ModelProvider.extract_media(request) -> StructuredModelResult`

`ExaSearchProvider` is the only search provider for all three search variants.
Results mode requests bounded highlights for snippets. Answer mode requests up
to `max_results` candidates, retrieves at most the top three through SmartFetch,
and gives Gemini only those bounded retrieved source records. Structured mode
requests up to `max_results` candidates, retrieves at most `max_sources` (1–3)
through SmartFetch, and gives Gemini only those records. SmartFetch does not
scrape Exa or expose the Exa response.

`GeminiProvider` uses the current recommended Google GenAI Interactions API.
Server-controlled routing chooses either stable `gemini-3.8-flash` or stable
`gemini-3.5-flash-lite` for each variant after the benchmark. The caller cannot
select a model or thinking level. Generation is one non-streaming call with a
bounded output token limit and server-controlled low thinking effort where
supported, with no automatic retry. Gemini 3.8 Flash uses `low`; `minimal` is
unsupported and must not be sent. Model-specific settings are validated against
the official API before benchmarking.

Every Gemini `interactions.create` request must explicitly set `store=false`;
API defaults are never sufficient. V1.11 provider adapters must not send
`previous_interaction_id` or enable `background=true`. No Gemini prompt,
schema, retrieved content, media analysis, model output, or interaction object
may be intentionally stored for conversational reuse. These stateless-request
requirements are separate from Gemini Files API upload and deletion: file
deletion does not replace `store=false`, and `store=false` does not delete an
uploaded file. Both protections apply when a request uses the Files API.

Outbound-request contract tests must enforce these settings across every
Gemini adapter path. SDK updates or refactors must fail those tests if they
omit or change stateless mode; no fallback to a storage-enabled request is
permitted.

Only these environment variables are new secrets:

- `EXA_API_KEY`
- `GEMINI_API_KEY`

They are server-side only, represented with redacted dataclass fields, never
placed in URLs, and never accepted from a caller. Error conversion occurs in
the provider adapters and returns only finite internal codes. Provider clients
must not install SDK debug handlers that log request headers or bodies.

Provider readiness has two levels:

1. Missing/empty configuration, client-construction failure, or an already-open
   local provider circuit returns `provider_unavailable` before selecting a
   paid wrapper. There is no challenge, signing, retrieval, or settlement.
2. A previously unknown outage or bad credential can be discovered only after
   a verified request reaches the provider. That request returns a safe error
   and does not settle. The failure opens a short local circuit so subsequent
   calls fail before a challenge. The system cannot honestly guarantee advance
   knowledge of every provider outage without making provider calls before
   payment.

No network provider probe runs at startup or per unpaid request. Such a probe
would violate the no-provider-work-before-authorization boundary and make
startup dependent on external availability.

### 9.2 Model selection

No single Gemini model is permanently assigned in the design. The benchmark
compares stable `gemini-3.8-flash` with stable
`gemini-3.5-flash-lite` independently for:

- Exa-sourced cited answers;
- structured multi-source text extraction;
- webpage extraction;
- image extraction;
- PDF extraction;
- audio extraction;
- low-resolution video extraction.

Selection is based on contract-valid and evidence-valid accuracy first, then
observed worst-case cost, median cost, and latency. Production uses a checked-in
server-controlled allowlisted routing table. An optional environment override
may select only one of the two approved stable model IDs; it cannot accept
arbitrary model names. January 1, 2027 standard prices are used for margin
decisions even if the benchmark runs during promotional pricing:

- Gemini 3.8 Flash: `$1.50`/M input tokens and `$7.50`/M output/thinking tokens;
- Gemini 3.5 Flash-Lite: `$0.30`/M multimodal input tokens and `$2.50`/M
  output/thinking tokens.

Verified against official Google documentation on September 5, 2026: Gemini
3.8 Flash is stable and has the same listed per-token pricing as 3.7 Flash,
but can use more reasoning tokens on complex tasks. Equal token prices do not
imply equal cost per result. Routing must compare measured contract-valid and
evidence-valid accuracy and actual total token cost per successful result,
including input, output, and reasoning tokens without double-counting usage.
The benchmark remains exactly these two models; Gemini 3.8 Flash Cyber is
outside SmartFetch's use case and is not a candidate or allowed override.

Exa costs are added to both answer and structured-search model costs. Free
quotas and promotional discounts are excluded. The answer and structured
variants cannot deploy at their provisional prices unless their combined Exa,
retrieval, Gemini, and non-settled failure costs pass the release gate.

### 9.3 Media download and inspection

Gemini never receives a caller URL. SmartFetch first performs an HTTPS-only
streaming download using the same public-target and per-redirect SSRF
validation. Transport selection occurs only after local MIME and limit checks.

Inline Gemini requests have a total request-size limit below 20 MB once base64
expansion, schema, prompt, and instructions are included. SmartFetch computes
the complete serialized request size before submission and uses inline data
only when it is at most 18,000,000 bytes, leaving deterministic headroom below
the provider limit. Larger permitted PDF, audio, and video inputs use the
Gemini Files API. Images remain inline under the 10 MiB image cap; an image that
cannot fit the complete inline request fails safely rather than being uploaded.

Limits are checked from declared length, streamed byte count, MIME signature,
and local inspection before Gemini invocation:

| Type | Allowed MIME families | Byte cap | Additional cap | Gemini resolution |
|---|---|---:|---|---|
| image | JPEG, PNG, WebP | 10 MiB | 20 megapixels | benchmark `medium` vs `high` |
| PDF | `application/pdf` | 20 MiB | 20 pages | `medium` |
| audio | MP3, WAV, M4A/AAC, Ogg, FLAC | 25 MiB | 30 minutes | native audio input |
| video | MP4, WebM, QuickTime | 50 MiB | 10 minutes | explicitly `low` |

All types permit at most five redirects. Connect timeout is 5 seconds, read
timeout 15 seconds, and download wall time 25 seconds. Variant wall times are
bounded independently: results 15 seconds, answer 40 seconds, structured
search 60 seconds, webpage/image/PDF 60 seconds, audio 90 seconds, and video
120 seconds. These limits apply only to new capabilities and do not modify
V1.10.6 timeouts.

MIME validation requires agreement among the allowlisted `Content-Type`, magic
bytes/container probe, and path-independent local inspector. A generic or
contradictory MIME fails with `unsupported_media_type`. Inspection uses Pillow
for images, pypdf for page count, and a sandboxed `ffprobe` subprocess for
audio/video duration and container metadata. `ffprobe` receives a generated
temporary filename, never a URL. It has a 5-second subprocess timeout, no shell,
no network, bounded output capture, and a minimal environment.

Temporary files are created with restrictive permissions in the process temp
directory, never inside the repository, and deleted in `finally` on success,
failure, timeout, and cancellation. For a Files API request, SmartFetch keeps
the returned provider file identity only in private request-local memory and
deletes it in the same `finally` block. Deletion is confirmed through the
SDK's documented deletion result; if that result is not conclusive, SmartFetch
performs one bounded read-back and requires a not-found result. Provider file
names and URIs never enter logs, activity metadata, exceptions, or responses.

If immediate provider deletion cannot be confirmed, the handler returns
`provider_cleanup_failed`, does not deliver a success result, does not settle,
and opens a short Gemini circuit. The bounded public message states only that
Google automatically expires uploaded files after up to 48 hours. There is no
cleanup retry. Media bytes and derived text are never cached. Download, media,
search, and model concurrency use separate bounded semaphores so new workloads
cannot exhaust the existing fetch/browser pools.

## 10. Payment and execution data flows

### 10.1 REST

```text
exact finite route
  -> local provider-config/circuit gate
  -> static x402 challenge or authorization verification
  -> validate bounded body/schema/filter
  -> provider/download/retrieval work
  -> normalize and locally validate result + evidence
  -> successful HTTP response
  -> official x402 settlement and PAYMENT-RESPONSE
```

Provider readiness must run outside/before the x402 route middleware for only
the new exact paths. Existing `/fetch` middleware behavior is untouched. When
configuration is present and the circuit is closed, the existing shared
resource server and normal x402 middleware enforce each static route.

Errors after verification return status 400 or greater, so the HTTP lifecycle
does not settle. Input parsing never starts provider work. An unpaid valid
route receives exactly one challenge. No server-side paid retry occurs.

### 10.2 MCP

```text
free MCP initialize/tools-list
  -> tool input and cross-field validation
  -> local provider-config/circuit gate
  -> select exact prebuilt static wrapper
  -> x402 challenge or authorization verification
  -> provider/download/retrieval work inside wrapped handler
  -> normalize and locally validate result + evidence
  -> successful CallToolResult
  -> official x402 settlement metadata
```

The registered dispatch function itself is not paid. It performs only bounded
local validation and readiness checks, then invokes exactly one paid internal
wrapper. All external work lives inside the wrapped handler. Wrapper maps are
immutable after startup and keyed by enum values, preventing shared mutable
state or request-to-request leakage.

A payment created for `$0.05` results mode cannot satisfy the `$0.10` answer or
`$0.15` structured requirement: each dispatch path verifies against its own
exact amount and resource. The same isolation applies to source types.

## 11. Privacy-safe activity logging

V1.11 reuses the existing request-scoped `ContextVar` and lifecycle events. It
adds only allowlisted categorical or numeric fields to existing events:

- `capability`: `search_and_extract` or `extract_structured_data`;
- `variant`: one of the eight finite variants;
- `provider`: `exa` or `gemini` (never an account/project);
- `model_route`: `flash` or `flash_lite` (not a caller-provided model string);
- bounded integers: result count, source count, input tokens, output tokens,
  search-query count, latency, and provider cost in integer micro-USD.

Cost/usage fields appear only after a provider result supplies them and are
clamped to finite bounds. The activity allowlist drops arbitrary fields.

Logs never contain:

- search query, extraction instructions, schema, source URL/hostname, source
  content, snippets, citations, evidence, media, or extracted output;
- provider request/response objects or exception messages;
- API keys, project IDs, payment headers/proofs/signatures, wallet/payee values,
  authorization headers, cookies, or request bodies.

Only existing privacy-safe retrieval diagnostics may include a normalized
target hostname on a final webpage retrieval failure. No new logging path
weakens that policy. Logging is total: failures in usage extraction or event
emission cannot change the client response or settlement decision.

## 12. Discovery and versioning

V1.11 bumps `SERVICE_VERSION`, `server.json`, documentation, and required
assertions to `1.11.0` only when production implementation is complete.

Discovery behavior:

- OpenAPI declares all eight concrete REST paths, their exact static prices,
  request/response schemas, `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, and
  `PAYMENT-RESPONSE` locations, success/error examples, and no wildcard path.
- `/.well-known/x402` retains the existing `/fetch` entry unchanged and adds
  one resource per configured/available concrete route. Each resource derives
  scheme, network, asset, amount, and payee from generated runtime
  `PaymentRequirements`, not duplicated constants.
- Bazaar retains all existing HTTP and MCP records. It adds eight distinct HTTP
  resources and eight variant-specific MCP payment resource identities. MCP
  entries use `type: mcp`, the public tool name, Streamable HTTP transport, and
  an input example containing the exact variant.
- `/meta` lists exactly six public MCP tools and a bounded `capabilities`
  section showing the finite variants, current price, and configured
  availability. It does not expose keys or payee secrets.
- `/docs`, `/llms.txt`, README, robots, and sitemap link the two capability
  families and explain that generic live search is
  `search_and_extract(mode="results")` or
  `POST /search-and-extract/results`.
- `server.json` remains one remote Streamable HTTP MCP server; only version and
  approved description/tool documentation change. Registry publication is a
  separate, explicitly authorized release action.
- x402scan submission/update happens only after an explicitly authorized
  production deployment. It is not part of implementation or tests.

When provider configuration is absent, free documentation may describe the
contract but runtime manifests must mark variants unavailable or omit them from
the active paid-resource list. They must never advertise a route as executable
and then expose it for free.

## 13. Benchmark and price gate

The benchmark is a separate, explicitly authorized paid activity. No Gemini,
Exa, production HTTP, or paid MCP call occurs while writing or implementing the
repository design.

The checked-in pre-release benchmark corpus contains 32 representative cases,
four per finite variant:

| Workload | Cases | Minimum failures/edge cases |
|---|---:|---:|
| generic search results | 4 | 1 |
| Exa-sourced cited answer | 4 | 1 |
| structured multi-source search | 4 | 1 |
| webpage extraction | 4 | 1 |
| image extraction | 4 | 1 |
| PDF extraction | 4 | 1 |
| audio extraction | 4 | 1 |
| video extraction | 4 | 1 |

Cases use redistributable/public fixtures and cover empty results, unavailable
sources, redirects, JavaScript pages, malformed schemas, missing fields,
conflicting evidence, oversized media metadata, provider timeouts, and invalid
provider output. Gemini workloads run against both approved model candidates.

For every case record:

- variant, success/failure, finite failure code;
- Exa search-query/request count and returned `costDollars` where available;
- input, output, thinking, tool-use, and modality tokens;
- provider cost computed from January 2027 standard rates;
- HTTP/browser retrieval use and elapsed time;
- schema and evidence validation outcome;
- source/result count and media duration/page count;
- contract-valid success and evidence-valid success;
- cost of non-settled failures attributable to the variant.

Four cases per variant cannot support a statistically meaningful P95. The
pre-release report therefore shows observed maximum cost, median cost, observed
maximum and median latency, contract-valid success rate, evidence-valid success
rate, raw provider cost, and failure-adjusted cost per successful delivery. The
release price must be at least three times the greater of:

1. the highest observed provider cost of a successful contract-valid delivery;
   and
2. the conservative failure-adjusted observed cost per successful delivery,
   which allocates all measured non-settled failure costs across successes.

Operational P95 cost and latency are calculated only after approximately 100
real calls have accumulated for a variant. Until then, release and pricing
decisions use the conservative observed worst case, not a P95 label.

Free quotas and promotional 2026 prices are ignored. A hard benchmark budget,
provider accounts, keys, and explicit paid-call approval are required before
execution. The benchmark runner must stop at the authorized budget and cannot
retry automatically.

Known preliminary economics:

- Exa search is currently `$0.007` for up to ten results. Requesting content or
  highlights adds about `$0.001` per page/content type. Five highlighted
  results therefore cost approximately `$0.012`; the provisional `$0.05`
  results price has nominal room for the 3× target but still requires measured
  validation.
- Answer and structured search each incur Exa search, SmartFetch retrieval, and
  Gemini costs. Their combined observed worst-case cost—not an assumed token
  average—determines whether `$0.10` and `$0.15` are viable.
- Media token cost depends materially on duration, resolution, thinking, and
  output size. Audio and video prices cannot be finalized from byte limits
  alone.

These observations are blockers to deployment, not reasons to alter the
approved design prices silently.

## 14. Dependencies and infrastructure

No dependency changes occur in this design commit. Expected implementation
dependencies, pinned after compatibility checks, are:

- official `google-genai` Python SDK for the Interactions API;
- official `exa-py` Python SDK, or Exa's documented HTTPS API via the existing
  `requests` dependency if the SDK adds avoidable transitive risk;
- `jsonschema` for local final validation;
- Pillow for image dimensions/signature validation;
- pypdf for bounded PDF page inspection;
- system `ffprobe` from a pinned Debian `ffmpeg` package for audio/video
  duration and container validation.

The Gemini Files API is a deliberate transient-media dependency for permitted
PDF/audio/video inputs whose complete encoded inline request would exceed the
18,000,000-byte SmartFetch threshold. Its upload identity stays request-local,
its deletion is mandatory before success can be returned, and Google's
documented automatic expiration of uploaded files after up to 48 hours is the
bounded fallback if immediate deletion cannot be confirmed.

The implementation PR must resolve and freeze compatible stable versions
without upgrading x402, CDP SDK, FastAPI, Uvicorn, MCP, or unrelated packages.
If that cannot be done, implementation stops for approval.

Adding `ffprobe` increases the container image and attack surface. Omitting it
would make the pre-Gemini audio/video duration guarantee unreliable, so
audio/video cannot ship without this or an equivalently bounded local parser.

User-owned prerequisites for later implementation/benchmark/deployment:

- an Exa account/project and server-side `EXA_API_KEY` with an explicit spend
  ceiling;
- a Google AI/Gemini API project and server-side `GEMINI_API_KEY`, billing
  enabled, logging/data-use settings reviewed, and an explicit spend ceiling;
- explicit authorization and budget for the paid benchmark;
- later approval of final prices and model routing;
- later Railway secret configuration and deployment authorization;
- later MCP Registry and x402scan publication/update authorization.

No seller or buyer wallet change, private key, recovery phrase, or new x402
credential is required.

## 15. Test-first verification plan

Each implementation stage starts with failing tests and proves green before
the next stage.

### Contracts and schema guard

- Exact REST and MCP schemas, defaults, finite enums, unknown-property
  rejection, cross-field rules, domain/freshness normalization, and response
  caps.
- Structured search proves `max_sources` is 1–3, defaults to 3, cannot exceed
  `max_results`, and controls the exact number of SmartFetch retrievals.
- Property-based/fuzz tests for schema byte size, depth, property count,
  arrays, enums, nullable types, unknown keywords, recursion, remote refs,
  malicious keys, huge integers, non-finite numbers, and pathological nesting.
- Tests proving no schema or instruction is evaluated, imported, formatted, or
  resolved over the network.

### Payment gating

- Every REST path independently returns an unpaid challenge with the expected
  exact price, network, asset, payee, and resource.
- Each internal MCP variant independently returns the expected price and exact
  resource URI.
- An authorization for a cheaper variant is rejected by every more expensive
  variant before handler execution.
- Unknown path variants and invalid MCP enum values produce no challenge,
  provider call, retrieval, download, model call, or settlement.
- Missing provider configuration and an open circuit produce no challenge.
- Valid mocked payment verifies once, executes once, and settles once only
  after contract and evidence validation.
- Provider, download, retrieval, schema, evidence, timeout, cancellation, and
  serialization failures do not settle.
- No automatic paid retry occurs.

### Providers and model routing

- Adapter contract tests inspect the actual serialized outbound request at a
  mocked transport boundary for every Gemini path: `store` is explicitly
  boolean `false`, `previous_interaction_id` is absent, and `background` is
  absent or explicitly boolean `false`. No provider network call is made.
  These assertions remain mandatory on SDK upgrades and refactors, including
  Files API-backed analysis; checking only an isolated settings object is not
  sufficient.
- Contract tests use recorded synthetic provider objects with secrets and
  canaries to prove normalization and redaction.
- Exa request parameters, result count, domains, freshness, highlights, timeout,
  no retry, and cost extraction are bounded.
- Exa is called for results, answer, and structured search; Gemini has no
  enabled tools in any V1.11 mode.
- Both allowlisted model IDs are selectable only by server routing; caller model
  input is rejected.
- Output tokens/thinking and timeouts are capped, malformed outputs fail safely,
  and Gemini citation IDs must resolve to the Exa-selected, SmartFetch-retrieved
  source registry.
- Provider SDK logs and exceptions cannot expose keys or bodies.

### Retrieval and media

- `auto` and `always` exercise the real retrieval boundary and prove current
  existing-call behavior is unchanged; no HTTP-only core path is added.
- Search structured mode retrieves at most three sources through the existing
  engine, including HTTP-to-browser fallback.
- Initial and redirected media URLs pass the existing public-target policy.
- MIME spoofing, declared/streamed oversize, excess pages/pixels/duration,
  redirect loops, slow streams, decompression bombs, corrupt containers, and
  temp-file cleanup are covered.
- Over-limit PDF/audio/video fails before Gemini and without settlement.
- Inline-size accounting includes base64, schema, prompt, instructions, and
  serialization overhead. Larger permitted PDF/audio/video uses the Files API,
  deletes the provider file in `finally`, and cannot settle when immediate
  deletion is unconfirmed. Provider file identities never reach output/logs.
- Semaphore and timeout tests prove new pools cannot consume existing fetch or
  browser capacity.

### Evidence and public responses

- JSON parses and validates against the guarded caller schema locally.
- Non-null required leaves have valid bounded evidence; source IDs and locators
  resolve; text quotes occur in source text; pages/timestamps stay in range.
- A schema-permitted missing/null field, disclosed in `missing_fields` and
  `uncertainties`, alongside usable evidenced non-null requested data succeeds
  and is settlement eligible through both mocked HTTP and MCP payment flows.
- A missing required non-nullable field fails without settlement.
- A null absent from `missing_fields` fails without settlement.
- A null without a corresponding bounded uncertainty fails without settlement.
- A non-null required leaf without valid evidence fails without settlement.
- All requested values missing/null fail with the finite evidence/validation
  error contract without settlement, even when every null is schema-permitted
  and disclosed. Exercise nested RFC 6901 pointers as well as top-level fields.
- Provider objects and unsupported fields are absent from public output.
- All failures use finite codes and retain the same public envelope in REST and
  MCP.

### Logging and privacy

- Activity correlation keeps the existing `request_id` across payment and tool
  events.
- Concurrent calls do not leak variant, usage, source, or request state.
- Canary scans prove no query, instructions, schema, full URL/path/query,
  source text, snippet, evidence, media, model output, provider key, payment
  data, signature, header, raw exception, or provider object reaches logs.
- Arbitrary fields are discarded and logging failures do not alter responses or
  settlement.

### Discovery and regression

- OpenAPI and `/.well-known/x402` list exactly the eight concrete variants with
  runtime-derived requirements; no wildcard is present.
- Bazaar HTTP and MCP resources remain distinct and variant-priced.
- `/meta` and MCP `tools/list` expose exactly six tools after release; the four
  existing names, schemas, descriptions, and order remain unchanged.
- Generic search is discoverable through `mode: results` and its concrete REST
  path.
- Existing `/fetch`, health, root, docs, llms, robots, sitemap, Glama,
  AgentCash metadata, Registry manifest, access-log redaction, activity,
  diagnostics, payment, API, MCP, security, browser, and local retrieval suites
  remain green.
- Full Python suite, security smoke, API smoke, MCP smoke, local 20/20 retrieval,
  npm `ci`/audit/type-check, secret scan, protected-file checks,
  `mcp-publisher validate server.json`, and `git diff --check` pass before any
  release PR is merged.

No real x402 payment or provider call is part of automated tests.

## 16. Staged V1.11 implementation sequence

All stages remain on V1.11 branches/PRs and none deploys independently. Each PR
is reviewable and preserves a passing main branch.

### PR 1 — contracts, schema guard, provider interfaces

- Add request/response models, finite variant table, schema subset guard,
  evidence validator, provider protocols, and synthetic unit tests.
- Add no live provider clients and expose no routes/tools.

### PR 2 — provider adapters and cost accounting

- Add pinned Google/Exa dependencies after compatibility approval.
- Implement adapters, server-controlled two-model allowlist, safe errors,
  circuit state, usage/cost accounting, and mocked contract tests.
- Still expose no production route/tool.

### PR 3 — safe media ingestion

- Add bounded media downloader/inspectors, inline-size accounting, transient
  Files API upload/deletion, and Docker `ffprobe` support.
- Map webpage `auto` and `always` directly to the existing retrieval engine.
- Prove existing retrieval behavior and protected security tests are unchanged.

### PR 4 — static HTTP and MCP payment integration

- Register the eight exact REST routes and eight internal MCP wrappers.
- Expose exactly the two new tools.
- Add payment-gating, cheaper-authorization, no-settlement, availability, and
  concurrency integration tests.

### PR 5 — discovery, version, docs, and benchmark harness

- Add runtime-derived OpenAPI, well-known x402, Bazaar, `/meta`, docs, llms,
  README, and `server.json` metadata.
- Bump the complete release to `1.11.0`.
- Add the benchmark corpus/runner, but do not execute it without paid approval.

### Separate release gate

- User supplies provider projects/keys through Railway secrets and authorizes a
  hard benchmark budget.
- Run the two-model benchmark, review accuracy/cost artifacts, approve model
  routing and final prices, then run the complete release gate.
- Deployment, one controlled unpaid validation, any paid smoke, MCP Registry
  publication, and x402scan update each require separate authorization.

## 17. Rollout and rollback

The new capabilities are guarded by server-controlled enablement plus provider
configuration. On deployment:

1. keep new capability enablement false while existing V1.10.6 routes boot;
2. validate free discovery and the unchanged four tools;
3. enable configured variants only after model routing and prices pass the
   benchmark gate;
4. verify free challenges only before any separately authorized paid smoke.

Rollback disables the new capability flag or redeploys the V1.10.6 commit.
Existing `/fetch` and four MCP tools do not depend on provider keys, media
packages, or new wrappers and remain operational. A provider circuit opening
removes execution availability for only affected new variants; it does not make
them free and does not alter existing resources.

## 18. Expected file changes

No files other than this design document change in the design phase.

Expected later additions:

- `smartfetch/v111_contracts.py`
- `smartfetch/schema_guard.py`
- `smartfetch/evidence.py`
- `smartfetch/provider_types.py`
- `smartfetch/providers/exa.py`
- `smartfetch/providers/gemini.py`
- `smartfetch/provider_health.py`
- `smartfetch/media.py`
- `smartfetch/costs.py`
- `smartfetch/v111_service.py`
- `smartfetch/v111_payments.py`
- focused `tests/test_v111_*.py`
- `benchmarks/v111_cases.json`
- `scripts/benchmark_v111.py`

Expected later modifications, kept narrow:

- `smartfetch/config.py` for V1.11 limits, provider configuration, and version;
- `smartfetch/payments.py` only to combine the existing `/fetch` config with the
  finite static V1.11 route configs using the same official resource server;
- `smartfetch/server.py` for thin endpoint wiring;
- `smartfetch/mcp_server.py` for two thin dispatch tools and eight static
  wrappers;
- `smartfetch/activity.py` for finite V1.11 allowlist fields;
- `smartfetch/bazaar.py` and `smartfetch/discovery.py` for generated metadata;
- `requirements.txt`, `Dockerfile`, `README.md`, `server.json`, and version-
  dependent tests/smokes.

`smartfetch/core.py`, payment verification/settlement internals, browser
implementation, SSRF policy, existing output projections, paid buyer examples,
Railway configuration, and GitHub configuration are not expected to change.
Any test that appears to require such a change stops the relevant
implementation PR for review.

## 19. Design blockers and decisions still required

The public route/tool shape is approved; this full draft still awaits final
approval. These gates remain before a production V1.11 release:

1. approve compatible pinned provider/media dependency versions after a clean
   resolver and vulnerability review;
2. approve adding `ffprobe` to the container image, or remove audio/video from
   V1.11;
3. supply server-side Exa and Gemini accounts/keys with spend ceilings;
4. authorize the paid benchmark and its hard maximum budget;
5. approve the measured per-variant Gemini routing table;
6. approve any price change required by the 3× observed-worst-case margin
   gate;
7. approve deployment and later external discovery publication separately.

## 20. Self-review

- **Ambiguity:** REST discriminators are in finite paths; MCP discriminators are
  finite enums. Unknown variants are explicitly free errors before a wrapper.
- **Payment correctness:** every variant has one static requirement; no cheaper
  `accepts` alternative is exposed; external work is inside the verified
  wrapper; failures do not settle.
- **Provider availability:** the design does not overpromise advance knowledge
  of a new outage. Missing configuration/open circuits fail before challenge;
  first runtime failures can follow verification but never settlement.
- **Secret handling:** only two new server-side keys exist; neither can enter a
  caller contract, URL, log, exception, discovery document, or repository file.
- **Schema safety:** support is explicitly a bounded subset, with no refs,
  recursion, regex, evaluation, or code execution.
- **Structured completeness:** disclosed schema-permitted nulls are valid only
  alongside usable non-null requested data; non-null required leaves need
  evidence, and failed/partial or all-missing/null results do not settle.
- **Interaction privacy:** all Gemini creation requests explicitly disable
  storage, prohibit conversational continuation and background execution, and
  retain outbound-request regression coverage independently of file cleanup.
- **Media safety:** limits are enforced before Gemini; URLs are never delegated
  to Gemini; temporary data is bounded and deleted; larger permitted
  PDF/audio/video inputs use the Files API only with mandatory request-local
  identity tracking and confirmed deletion.
- **Cost honesty:** provisional prices are not declared viable. January 2027
  rates, Exa plus Gemini costs, non-settled failure costs, and a 3× observed-
  worst-case gate are explicit. Operational P95 waits for approximately 100
  real calls per variant.
- **Scope:** exactly two public MCP tools and eight concrete REST routes are
  added. Existing tools and `/fetch` remain unchanged. No A2A, wallet, CAPTCHA,
  authenticated action, upload, free demo, search-provider scraping, or
  unrelated refactor is included.
- **Module size:** provider, schema, evidence, media, cost, and orchestration
  responsibilities are isolated; `server.py`, `mcp_server.py`, and
  `discovery.py` remain wiring/generation layers.

## 21. Primary references

- x402 seller quickstart and MCP discovery:
  <https://docs.x402.org/getting-started/quickstart-for-sellers>
- x402 MCP server guide:
  <https://docs.x402.org/guides/mcp-server-with-x402>
- x402 HTTP/payment lifecycle:
  <https://docs.x402.org/core-concepts/http-402>
- Gemini 3.8 Flash model:
  <https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash>
- Gemini 3.8 Flash release, reasoning, and migration guidance:
  <https://ai.google.dev/gemini-api/docs/latest-model>
- Gemini 3.5 Flash-Lite model:
  <https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite>
- Gemini pricing:
  <https://ai.google.dev/gemini-api/docs/pricing>
- Gemini structured output:
  <https://ai.google.dev/gemini-api/docs/structured-output>
- Gemini file input methods:
  <https://ai.google.dev/gemini-api/docs/file-input-methods>
- Gemini media resolution:
  <https://ai.google.dev/gemini-api/docs/media-resolution>
- Exa search API:
  <https://exa.ai/docs/reference/search>
- Exa API pricing:
  <https://exa.ai/pricing?tab=api>
