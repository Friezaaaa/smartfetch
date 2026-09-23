"""Free public discovery documents for SmartFetch."""

from copy import deepcopy
from html import escape
from decimal import Decimal
from xml.etree import ElementTree

from x402.http.utils import encode_payment_required_header
from x402.schemas import PaymentRequired, PaymentRequirements, ResourceInfo

from .bazaar import (
    FETCH_DESCRIPTION,
    FETCH_INPUT_EXAMPLE,
    FETCH_INPUT_SCHEMA,
    FETCH_OUTPUT_EXAMPLE,
    FETCH_OUTPUT_SCHEMA,
    FETCH_TAGS,
    fetch_discovery_extension,
)
from .config import SERVICE_NAME, SERVICE_VERSION
from .media import MEDIA_LIMITS
from .v111_contracts import (
    DirectExtractionRequest,
    FailureResponse,
    SearchAndExtractRequest,
    SearchAnswerResponse,
    SearchResultsResponse,
    StructuredResponse,
    V111_VARIANTS,
)
from .v111_service import V111_DEADLINES_SECONDS


GITHUB_URL = "https://github.com/Friezaaaa/smartfetch"
PYTHON_EXAMPLE_URL = (
    f"{GITHUB_URL}/blob/main/examples/python/paid_mcp_client.py"
)
TYPESCRIPT_EXAMPLE_URL = (
    f"{GITHUB_URL}/blob/main/examples/typescript/paid-mcp-client.ts"
)
PYTHON_HTTP_EXAMPLE_URL = (
    f"{GITHUB_URL}/blob/main/scripts/paid_fetch_mainnet_test.py"
)
OFFICIAL_X402_BUYER_URL = (
    "https://docs.x402.org/getting-started/quickstart-for-buyers"
)
TOOL_NAMES = (
    "fetch_webpage",
    "webpage_to_markdown",
    "extract_webpage_text",
    "render_webpage",
)
V111_TOOL_NAMES = (
    "search_and_extract",
    "extract_structured_data",
)
AGENTCASH_GUIDANCE = (
    "Use POST /fetch as a paid fallback when ordinary access to a public "
    "HTTP(S) webpage fails or is incomplete, when JavaScript rendering is "
    "required, or when clean text or Markdown is needed. Send url and "
    "optional max_chars and force_browser. Do not use SmartFetch to bypass "
    "CAPTCHAs, authentication, paywalls, robots restrictions, or access "
    "controls."
)


def public_base_url(request):
    """Return the framework-resolved external scheme and authority."""
    return f"{request.url.scheme}://{request.url.netloc}"


def public_urls(request):
    """Build all public links from the proxy-aware request URL."""
    base = public_base_url(request)
    return {
        "base": base,
        "x402": f"{base}/.well-known/x402",
        "docs": f"{base}/docs",
        "openapi": f"{base}/openapi.json",
        "llms": f"{base}/llms.txt",
        "robots": f"{base}/robots.txt",
        "sitemap": f"{base}/sitemap.xml",
        "meta": f"{base}/meta",
        "fetch": f"{base}/fetch",
        "mcp": f"{base}/mcp",
    }


def _v111_requirements(v111_accepts):
    """Return a complete variant requirement map or fail closed to disabled."""
    if type(v111_accepts) is not dict:
        return {}
    requirements = {}
    for definition in V111_VARIANTS:
        accepts = v111_accepts.get(definition.mcp_resource)
        if type(accepts) is not list or len(accepts) != 1:
            return {}
        requirement = accepts[0]
        if not isinstance(requirement, PaymentRequirements):
            return {}
        requirements[definition.variant] = requirement
    return requirements


def x402_manifest(urls, settings, v111_accepts=None):
    """Return a community x402 service manifest without payment secrets."""
    requirements = _v111_requirements(v111_accepts)
    manifest = {
        "spec": "agent402-service-manifest/1",
        "version": 1,
        "name": SERVICE_NAME,
        "summary": (
            "Reliable public-web retrieval for AI agents: URL in, clean "
            "text, Markdown, links, and metadata out."
        ),
        "homepage": urls["base"],
        "repository": GITHUB_URL,
        "resources": [urls["fetch"]],
        "payment": {
            "protocol": "x402",
            "x402Version": 2,
            "enabled": settings.enabled,
            "scheme": "exact",
            "price": settings.price,
            "network": settings.network,
            "asset": "USDC",
        },
        "endpoints": {
            "mcp": {
                "url": urls["mcp"],
                "transport": "streamable-http",
            },
            "openapi": urls["openapi"],
            "llms": urls["llms"],
            "docs": urls["docs"],
            "metadata": urls["meta"],
        },
    }
    if requirements:
        manifest["resources"].extend(
            f"{urls['base']}{definition.rest_path}"
            for definition in V111_VARIANTS
        )
        manifest["v111"] = {
            "http": [
                _variant_manifest_entry(
                    definition,
                    requirements[definition.variant],
                    f"{urls['base']}{definition.rest_path}",
                )
                for definition in V111_VARIANTS
            ],
            "mcp": [
                _variant_manifest_entry(
                    definition,
                    requirements[definition.variant],
                    definition.mcp_resource,
                )
                for definition in V111_VARIANTS
            ],
        }
    return manifest


def _variant_manifest_entry(definition, requirement, resource):
    return {
        "capability": definition.capability,
        "variant": definition.variant,
        "resource": resource,
        "price": _usd_price_from_atomic(requirement.amount),
        "scheme": requirement.scheme,
        "network": requirement.network,
        "amount": requirement.amount,
        "asset": requirement.asset,
        "assetSymbol": "USDC",
        "payTo": requirement.pay_to,
    }


def _error_response(description, example=None):
    response = {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean", "const": False},
                        "error_code": {"type": "string"},
                        "error": {"type": "string"},
                        "request_id": {"type": "string"},
                    },
                    "required": ["success", "error", "request_id"],
                },
            },
        },
    }
    if example is not None:
        response["content"]["application/json"]["example"] = example
    return response


def _atomic_usdc_amount(price):
    return str(int(Decimal(price.removeprefix("$")) * 1_000_000))


def _agentcash_usd_amount(price):
    return f"{Decimal(price.removeprefix('$')):.6f}"


def _usd_price_from_atomic(amount):
    value = Decimal(amount) / 1_000_000
    if value >= Decimal("0.01"):
        return f"${value:.2f}"
    exact = format(value, "f")
    return f"${exact.rstrip('0').rstrip('.')}"


def _payment_details(settings=None, payment_requirement=None):
    price = (
        _usd_price_from_atomic(payment_requirement.amount)
        if payment_requirement is not None
        else settings.price if settings is not None else "$0.005"
    )
    network = (
        payment_requirement.network
        if payment_requirement is not None
        else settings.network if settings is not None else "eip155:84532"
    )
    return {
        "price": price,
        "scheme": (
            payment_requirement.scheme
            if payment_requirement is not None
            else "exact"
        ),
        "network": network,
        "asset": (
            payment_requirement.asset
            if payment_requirement is not None
            else "USDC"
        ),
    }


def _payment_required_example(urls, payment_requirement):
    if not isinstance(payment_requirement, PaymentRequirements):
        return None
    challenge = PaymentRequired(
        x402Version=2,
        resource=ResourceInfo(
            url=urls["fetch"],
            description=FETCH_DESCRIPTION,
            mimeType="application/json",
            serviceName=SERVICE_NAME,
            tags=FETCH_TAGS,
        ),
        accepts=[payment_requirement],
        extensions=fetch_discovery_extension(),
    )
    return encode_payment_required_header(challenge)


def openapi_document(
    urls,
    settings,
    payment_requirement=None,
    v111_accepts=None,
):
    """Return the explicit public OpenAPI 3.1 contract for POST /fetch."""
    atomic_amount = (
        payment_requirement.amount
        if payment_requirement is not None
        else _atomic_usdc_amount(settings.price)
    )
    network = (
        payment_requirement.network
        if payment_requirement is not None
        else settings.network
    )
    scheme = (
        payment_requirement.scheme
        if payment_requirement is not None
        else "exact"
    )
    asset = (
        payment_requirement.asset
        if payment_requirement is not None
        else None
    )
    network_name = (
        "Base mainnet" if network == "eip155:8453" else "Base Sepolia"
    )
    x402_contract = {
        "x402Version": 2,
        "scheme": scheme,
        "network": network,
        "assetSymbol": "USDC",
        "price": settings.price,
        "amount": atomic_amount,
    }
    if asset is not None:
        x402_contract["asset"] = asset
    payment_required_header = {
        "description": (
            "Base64-encoded x402 v2 PaymentRequired challenge. Decode and "
            "validate it before signing."
        ),
        "schema": {"type": "string"},
    }
    payment_required_example = _payment_required_example(
        urls,
        payment_requirement,
    )
    if payment_required_example is not None:
        payment_required_header["example"] = payment_required_example
    document = {
        "openapi": "3.1.0",
        "info": {
            "title": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "description": (
                "Read, fetch, scrape, extract, and browser-render public "
                "webpages into agent-ready text, Markdown, links, and metadata."
            ),
            "x-guidance": AGENTCASH_GUIDANCE,
        },
        "servers": [{"url": urls["base"]}],
        "externalDocs": {
            "description": "SmartFetch discovery documentation",
            "url": urls["docs"],
        },
        "paths": {
            "/fetch": {
                "post": {
                    "operationId": "fetchWebpage",
                    "summary": "Fetch a public webpage",
                    "description": (
                        "Paid x402 exact retrieval of one public HTTP or HTTPS "
                        f"URL. The configured price is {settings.price} per "
                        f"execution using USDC on {network_name}."
                    ),
                    "x-x402": x402_contract,
                    "x-payment-info": {
                        "price": {
                            "mode": "fixed",
                            "currency": "USD",
                            "amount": _agentcash_usd_amount(settings.price),
                        },
                        "protocols": [{"x402": {}}],
                    },
                    "parameters": [
                        {
                            "name": "PAYMENT-SIGNATURE",
                            "in": "header",
                            "required": False,
                            "description": (
                                "Base64-encoded x402 v2 PaymentPayload on the "
                                "paid retry request."
                            ),
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": FETCH_INPUT_SCHEMA,
                                "example": FETCH_INPUT_EXAMPLE,
                            },
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Successful SmartFetch result",
                            "headers": {
                                "PAYMENT-RESPONSE": {
                                    "description": (
                                        "Base64-encoded x402 v2 "
                                        "SettlementResponse after successful "
                                        "settlement."
                                    ),
                                    "schema": {"type": "string"},
                                },
                            },
                            "content": {
                                "application/json": {
                                    "schema": FETCH_OUTPUT_SCHEMA,
                                    "example": FETCH_OUTPUT_EXAMPLE,
                                },
                            },
                        },
                        "400": _error_response(
                            "Invalid request or blocked target"
                        ),
                        "402": {
                            "description": "x402 v2 payment required",
                            "headers": {
                                "PAYMENT-REQUIRED": payment_required_header,
                            },
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "request_id": {"type": "string"},
                                        },
                                        "required": ["request_id"],
                                        "additionalProperties": False,
                                    },
                                    "example": {
                                        "request_id": "a1b2c3d4e5f60708",
                                    },
                                },
                            },
                        },
                        "429": _error_response("Rate limit exceeded"),
                        "502": _error_response(
                            "Upstream fetch failed",
                            {
                                "success": False,
                                "error_code": "fetch_failed",
                                "error": "Retrieval failed",
                                "request_id": "a1b2c3d4e5f60708",
                            },
                        ),
                        "503": _error_response("Service is at capacity"),
                        "504": _error_response("Retrieval timed out"),
                    },
                },
            },
        },
    }
    requirements = _v111_requirements(v111_accepts)
    if requirements:
        for definition in V111_VARIANTS:
            document["paths"][definition.rest_path] = {
                "post": _v111_openapi_operation(
                    urls,
                    definition,
                    requirements[definition.variant],
                ),
            }
    return document


def _schema_without_fields(model, *fields):
    schema = deepcopy(model.model_json_schema())
    properties = schema.get("properties", {})
    for field in fields:
        properties.pop(field, None)
    required = schema.get("required")
    if type(required) is list:
        schema["required"] = [field for field in required if field not in fields]
    return schema


def _v111_request_contract(definition):
    if definition.capability == "search_and_extract":
        removed = ["mode"]
        if definition.variant != "structured":
            removed.extend(("max_sources", "json_schema", "instructions"))
        schema = _schema_without_fields(SearchAndExtractRequest, *removed)
        example = {
            "query": "official product release details",
            "max_results": 5,
        }
        if definition.variant == "structured":
            example.update({
                "max_sources": 3,
                "json_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            })
        return schema, example
    removed = ["source_type"]
    if definition.variant != "webpage":
        removed.append("render_mode")
    schema = _schema_without_fields(DirectExtractionRequest, *removed)
    example = {
        "source_url": "https://example.com/public-source",
        "json_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
    }
    if definition.variant == "webpage":
        example["render_mode"] = "auto"
    return schema, example


def _v111_response_model(definition):
    if definition.variant == "results":
        return SearchResultsResponse
    if definition.variant == "answer":
        return SearchAnswerResponse
    return StructuredResponse


def _v111_payment_required_header(urls, definition, requirement):
    challenge = PaymentRequired(
        x402Version=2,
        resource=ResourceInfo(
            url=f"{urls['base']}{definition.rest_path}",
            description=f"SmartFetch {definition.capability} {definition.variant}",
            mimeType="application/json",
            serviceName=SERVICE_NAME,
            tags=["search", "extraction", definition.variant],
        ),
        accepts=[requirement],
    )
    return {
        "description": (
            "Base64-encoded x402 v2 PaymentRequired challenge for this "
            "exact static variant."
        ),
        "schema": {"type": "string"},
        "example": encode_payment_required_header(challenge),
    }


def _v111_openapi_operation(urls, definition, requirement):
    request_schema, request_example = _v111_request_contract(definition)
    price = _usd_price_from_atomic(requirement.amount)
    payment = {
        "x402Version": 2,
        "scheme": requirement.scheme,
        "network": requirement.network,
        "asset": requirement.asset,
        "assetSymbol": "USDC",
        "price": price,
        "amount": requirement.amount,
        "payTo": requirement.pay_to,
        "resource": definition.rest_path,
    }
    responses = {
        "200": {
            "description": "Validated successful result",
            "headers": {"PAYMENT-RESPONSE": {
                "description": (
                    "Base64-encoded x402 v2 SettlementResponse after "
                    "successful settlement."
                ),
                "schema": {"type": "string"},
            }},
            "content": {"application/json": {
                "schema": _v111_response_model(definition).model_json_schema(),
            }},
        },
        "402": {
            "description": "x402 v2 payment required",
            "headers": {"PAYMENT-REQUIRED": _v111_payment_required_header(
                urls,
                definition,
                requirement,
            )},
            "content": {"application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                    "required": ["request_id"],
                    "additionalProperties": False,
                },
                "example": {"request_id": "a1b2c3d4e5f60708"},
            }},
        },
    }
    for status, description in {
        "400": "Invalid request or blocked source",
        "413": "Request or media exceeds a fixed limit",
        "415": "Unsupported media type",
        "422": "Provider output, schema, or evidence validation failed",
        "502": "Provider, search, or retrieval failed",
        "503": "Provider or service capacity unavailable",
        "504": "Fixed operation deadline exceeded",
    }.items():
        responses[status] = {
            "description": description,
            "content": {"application/json": {
                "schema": FailureResponse.model_json_schema(),
            }},
        }
    return {
        "operationId": (
            f"{definition.capability}_{definition.variant}".replace("_", "-")
        ),
        "summary": f"{definition.capability} ({definition.variant})",
        "description": (
            f"Paid {definition.variant} variant with a fixed {price} x402 "
            "requirement. Payment is verified before external work and is "
            "settled only after validated successful delivery."
        ),
        "x-x402": payment,
        "x-payment-info": {
            "price": {
                "mode": "fixed",
                "currency": "USD",
                "amount": _agentcash_usd_amount(price),
            },
            "protocols": [{"x402": {}}],
        },
        "parameters": [{
            "name": "PAYMENT-SIGNATURE",
            "in": "header",
            "required": False,
            "description": "Base64-encoded x402 v2 PaymentPayload.",
            "schema": {"type": "string"},
        }],
        "requestBody": {
            "required": True,
            "content": {"application/json": {
                "schema": request_schema,
                "example": request_example,
            }},
        },
        "responses": responses,
    }


def _variant_description(definition):
    descriptions = {
        "results": "Generic search results from Exa discovery",
        "answer": "Cited answer from retrieved public sources",
        "structured": "Structured search with schema and evidence validation",
        "webpage": "Structured extraction from a public webpage",
        "image": "Structured extraction from one validated image",
        "pdf": "Structured extraction from one validated PDF",
        "audio": "Structured extraction from validated audio",
        "video": "Structured extraction from validated video",
    }
    return descriptions[definition.variant]


def _variant_limit_text(definition):
    if definition.variant == "results":
        return "1-10 results"
    if definition.variant == "answer":
        return "1-10 search results"
    if definition.variant == "structured":
        return "1-10 results; at most 3 retrieved sources"
    if definition.variant == "webpage":
        return "HTTPS public webpage; render_mode auto or always"
    limit = MEDIA_LIMITS[definition.variant]
    byte_limit = limit.max_bytes // (1024 * 1024)
    extras = []
    if limit.max_pixels is not None:
        extras.append(f"{limit.max_pixels // 1_000_000} megapixels")
    if limit.max_frames is not None:
        extras.append(f"{limit.max_frames} frame")
    if limit.max_pages is not None:
        extras.append(f"{limit.max_pages} pages")
    if limit.max_duration_seconds is not None:
        extras.append(f"{limit.max_duration_seconds // 60} minutes")
    return "; ".join([f"{byte_limit} MiB", *extras])


def _v111_docs_html(urls, requirements):
    if not requirements:
        return ""
    rows = []
    for definition in V111_VARIANTS:
        requirement = requirements[definition.variant]
        rows.append(
            "<tr>"
            f"<td><code>{escape(definition.rest_path)}</code></td>"
            f"<td><code>{escape(definition.mcp_resource)}</code></td>"
            f"<td>{escape(_variant_description(definition))}</td>"
            f"<td>{escape(_usd_price_from_atomic(requirement.amount))}</td>"
            f"<td>{escape(_variant_limit_text(definition))}</td>"
            f"<td>{V111_DEADLINES_SECONDS[definition.variant]:g}s</td>"
            "</tr>"
        )
    return """
    <h2>V1.11 paid search and structured extraction</h2>
    <p>When fully configured, SmartFetch exposes six MCP tools: the four legacy
    tools plus <code>search_and_extract</code> and
    <code>extract_structured_data</code>. Each concrete variant has a static,
    server-owned x402 resource and price.</p>
    <table>
      <thead><tr><th>REST</th><th>MCP resource</th><th>Capability</th><th>Price</th><th>Limits</th><th>Deadline</th></tr></thead>
      <tbody>""" + "".join(rows) + """</tbody>
    </table>
    <p>Generic results return normalized Exa sources. Cited answer output binds
    claims to retrieved sources. Structured search and webpage, image, PDF,
    audio, or video extraction validate the caller's bounded JSON Schema and
    require bounded supporting evidence for every non-null required leaf.
    Schema-permitted nullable values must appear in <code>missing_fields</code>
    with an uncertainty; an all-missing result fails.</p>
    <p>The fixed deadline covers the complete post-verification operation.
    Clients receive <code>PAYMENT-REQUIRED</code>, retry with
    <code>PAYMENT-SIGNATURE</code>, and receive <code>PAYMENT-RESPONSE</code>
    only after successful settlement. Invalid, partial, failed, empty, or
    unsafe results cause no settlement.</p>
    """


def docs_html(
    urls,
    settings=None,
    payment_requirement=None,
    v111_accepts=None,
):
    """Return concise human- and crawler-readable service documentation."""
    safe = {key: escape(value, quote=True) for key, value in urls.items()}
    github = escape(GITHUB_URL, quote=True)
    python_example = escape(PYTHON_EXAMPLE_URL, quote=True)
    typescript_example = escape(TYPESCRIPT_EXAMPLE_URL, quote=True)
    python_http_example = escape(PYTHON_HTTP_EXAMPLE_URL, quote=True)
    official_x402_buyer = escape(OFFICIAL_X402_BUYER_URL, quote=True)
    payment = {
        key: escape(str(value), quote=True)
        for key, value in _payment_details(
            settings,
            payment_requirement,
        ).items()
    }
    v111_section = _v111_docs_html(
        urls,
        _v111_requirements(v111_accepts),
    )
    v111_price_note = (
        " Enabled V1.11 variants use their individually listed $0.05, $0.10, "
        "$0.15 prices."
        if v111_accepts else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SmartFetch V{SERVICE_VERSION} discovery</title>
  <meta name="description" content="Public webpage reader, scraper, text extractor, Markdown converter, and browser renderer for AI agents.">
</head>
<body>
  <main>
    <h1>SmartFetch</h1>
    <p>SmartFetch is a webpage reader and fetch service for AI agents. It can scrape a public URL, extract clean text, convert a website to Markdown, preserve links and metadata, and use automatic browser rendering for JavaScript-heavy pages.</p>
    <p>Legacy <code>POST /fetch</code> and the four existing MCP tools use x402 <code>{payment['scheme']}</code> payments at <strong>{payment['price']}</strong> on <code>{payment['network']}</code> with asset <code>{payment['asset']}</code>.{v111_price_note} Discovery, health, metadata, MCP initialize, and MCP tools/list remain free.</p>
    <h2>Public endpoints</h2>
    <ul>
      <li><a href="{safe['meta']}">GET /meta</a> — machine-readable metadata</li>
      <li><a href="{safe['x402']}">GET /.well-known/x402</a> — community x402 service manifest</li>
      <li><a href="{safe['openapi']}">GET /openapi.json</a> — OpenAPI 3.1</li>
      <li><a href="{safe['llms']}">GET /llms.txt</a> — agent discovery summary</li>
      <li><a href="{safe['mcp']}">POST /mcp</a> — MCP Streamable HTTP</li>
      <li><code>POST {safe['fetch']}</code> — paid webpage retrieval</li>
    </ul>
    <h2>MCP tools</h2>
    <dl>
      <dt><code>fetch_webpage</code></dt><dd>Return the full SmartFetch text, Markdown, links, and metadata result.</dd>
      <dt><code>webpage_to_markdown</code></dt><dd>Return clean Markdown and core retrieval metadata without duplicate full text.</dd>
      <dt><code>extract_webpage_text</code></dt><dd>Return clean readable text and core retrieval metadata without Markdown.</dd>
      <dt><code>render_webpage</code></dt><dd>Force browser rendering and return the full SmartFetch result.</dd>
    </dl>
    <h2>Paying client examples</h2>
    <p>The examples list tools for free, enforce a $0.005 maximum payment, then perform the x402 challenge, payment, retry, and settlement flow. Running them can spend real Base-mainnet USDC.</p>
    <ul>
      <li><a href="{python_example}">Python paying MCP client</a></li>
      <li><a href="{typescript_example}">TypeScript paying MCP client</a></li>
    </ul>
    <h2>HTTP request example</h2>
    <pre><code>{{"url":"https://example.com/","max_chars":20000,"force_browser":false}}</code></pre>
    <h2>HTTP x402 buyer flow</h2>
    <ol>
      <li>Send the request without payment.</li>
      <li>Decode <code>PAYMENT-REQUIRED</code> and validate its scheme, network, asset, amount, and payee.</li>
      <li>Sign through an official capped x402 client, then retry with <code>PAYMENT-SIGNATURE</code>.</li>
      <li>After successful settlement, read <code>PAYMENT-RESPONSE</code>.</li>
    </ol>
    <p>SmartFetch verifies authorization before retrieval and settles only after successful delivery. Retrieval responses with status 400 or higher, including 502 failures, are returned without settlement.</p>
    <p>Never place a private key or recovery phrase in a URL, request body, log, example, command-line argument, or repository file. Never commit secret-bearing .env files. Use hidden interactive input, a platform-injected secret, or an approved wallet/secret-management service.</p>
    {v111_section}
    <ul>
      <li><a href="{python_http_example}">Tested Python HTTP buyer example</a></li>
      <li><a href="{official_x402_buyer}">Official x402 TypeScript and Python buyer guide</a></li>
    </ul>
    <p>Source and deployment documentation: <a href="{github}">{github}</a>.</p>
  </main>
</body>
</html>
"""


def _v111_llms_text(requirements):
    if not requirements:
        return ""
    lines = [
        "## V1.11 conditional capabilities",
        "When fully configured, SmartFetch exposes six MCP tools. The two "
        "additional tools are `search_and_extract` and "
        "`extract_structured_data`.",
        "",
    ]
    for definition in V111_VARIANTS:
        requirement = requirements[definition.variant]
        lines.append(
            f"- `{definition.rest_path}` / `{definition.mcp_resource}`: "
            f"{_variant_description(definition)}; "
            f"{_usd_price_from_atomic(requirement.amount)}; "
            f"{_variant_limit_text(definition)}; fixed "
            f"{V111_DEADLINES_SECONDS[definition.variant]:g}s deadline."
        )
    lines.extend([
        "",
        "Structured results require bounded evidence for each non-null "
        "required leaf. Schema-permitted nullable values use `missing_fields` "
        "plus an uncertainty; all-missing output fails.",
        "The x402 flow uses `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, and "
        "`PAYMENT-RESPONSE`. Invalid, partial, failed, empty, or unsafe "
        "results cause no settlement.",
        "",
    ])
    return "\n".join(lines)


def llms_text(
    urls,
    settings=None,
    payment_requirement=None,
    v111_accepts=None,
):
    """Return a compact llms.txt service summary."""
    payment = _payment_details(settings, payment_requirement)
    v111_section = _v111_llms_text(_v111_requirements(v111_accepts))
    v111_price_note = (
        " V1.11 variants use their individually listed $0.05, $0.10, and "
        "$0.15 prices."
        if v111_accepts else ""
    )
    return f"""# SmartFetch

SmartFetch reads, fetches, scrapes, and extracts public webpages for AI agents. It returns clean text, Markdown, links, and metadata, with browser rendering for JavaScript-heavy websites.

Payment: Legacy `POST /fetch` and the four existing MCP tools use {payment['price']} per execution with x402 {payment['scheme']} on {payment['network']} and asset {payment['asset']}.{v111_price_note}

## Endpoints
- [Community x402 manifest]({urls['x402']})
- [Documentation]({urls['docs']})
- [OpenAPI 3.1]({urls['openapi']})
- [Metadata]({urls['meta']})
- [Remote MCP Streamable HTTP]({urls['mcp']})
- [Paid HTTP retrieval]({urls['fetch']}): POST only

## MCP tools
- fetch_webpage: full SmartFetch result
- webpage_to_markdown: Markdown plus core metadata
- extract_webpage_text: clean text plus core metadata
- render_webpage: forced browser rendering plus the full result

## Paying MCP client examples
- [Python MCP example]({PYTHON_EXAMPLE_URL})
- [TypeScript MCP example]({TYPESCRIPT_EXAMPLE_URL})
- Both enforce a $0.005 maximum payment. Running them can spend real Base-mainnet USDC.

## HTTP buyer guidance
- [HTTP buyer example]({PYTHON_HTTP_EXAMPLE_URL})
- [Official x402 TypeScript and Python buyer guide]({OFFICIAL_X402_BUYER_URL})
- Send an unpaid request, validate PAYMENT-REQUIRED, sign with a capped official client, retry with PAYMENT-SIGNATURE, and read PAYMENT-RESPONSE after settlement.
- SmartFetch settles only after successful delivery; responses with status 400 or higher are returned without settlement.

{v111_section}

## Source
{GITHUB_URL}
"""


def robots_text(urls):
    """Allow public crawling and point crawlers to the sitemap."""
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {urls['sitemap']}\n"
    )


def sitemap_xml(urls):
    """Return a sitemap containing only free discovery content pages."""
    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    ElementTree.register_namespace("", namespace)
    root = ElementTree.Element(f"{{{namespace}}}urlset")
    for location in (
        urls["base"],
        urls["meta"],
        urls["docs"],
        urls["openapi"],
        urls["llms"],
    ):
        url = ElementTree.SubElement(root, f"{{{namespace}}}url")
        ElementTree.SubElement(url, f"{{{namespace}}}loc").text = location
    return ElementTree.tostring(
        root,
        encoding="unicode",
        xml_declaration=True,
    )
