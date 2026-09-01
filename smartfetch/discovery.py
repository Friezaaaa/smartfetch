"""Free public discovery documents for SmartFetch."""

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


def x402_manifest(urls, settings):
    """Return a community x402 service manifest without payment secrets."""
    return {
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
    value = format(Decimal(amount) / 1_000_000, "f")
    return f"${value.rstrip('0').rstrip('.')}"


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


def openapi_document(urls, settings, payment_requirement=None):
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
    return {
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


def docs_html(urls, settings=None, payment_requirement=None):
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
    <p>Each paid HTTP or MCP execution uses x402 <code>{payment['scheme']}</code> payments at <strong>{payment['price']}</strong> on <code>{payment['network']}</code> with asset <code>{payment['asset']}</code>. Discovery, health, metadata, MCP initialize, and MCP tools/list remain free.</p>
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
    <ul>
      <li><a href="{python_http_example}">Tested Python HTTP buyer example</a></li>
      <li><a href="{official_x402_buyer}">Official x402 TypeScript and Python buyer guide</a></li>
    </ul>
    <p>Source and deployment documentation: <a href="{github}">{github}</a>.</p>
  </main>
</body>
</html>
"""


def llms_text(urls, settings=None, payment_requirement=None):
    """Return a compact llms.txt service summary."""
    payment = _payment_details(settings, payment_requirement)
    return f"""# SmartFetch

SmartFetch reads, fetches, scrapes, and extracts public webpages for AI agents. It returns clean text, Markdown, links, and metadata, with browser rendering for JavaScript-heavy websites.

Payment: {payment['price']} per paid HTTP or MCP tool execution using x402 {payment['scheme']} on {payment['network']} with asset {payment['asset']}.

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
