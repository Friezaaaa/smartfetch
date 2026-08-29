"""Free public discovery documents for SmartFetch."""

from html import escape
from decimal import Decimal
from xml.etree import ElementTree

from .bazaar import (
    FETCH_INPUT_EXAMPLE,
    FETCH_INPUT_SCHEMA,
    FETCH_OUTPUT_EXAMPLE,
    FETCH_OUTPUT_SCHEMA,
)
from .config import SERVICE_NAME, SERVICE_VERSION


GITHUB_URL = "https://github.com/Friezaaaa/smartfetch"
PYTHON_EXAMPLE_URL = (
    f"{GITHUB_URL}/blob/main/examples/python/paid_mcp_client.py"
)
TYPESCRIPT_EXAMPLE_URL = (
    f"{GITHUB_URL}/blob/main/examples/typescript/paid-mcp-client.ts"
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


def _error_response(description):
    return {
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


def _atomic_usdc_amount(price):
    return str(int(Decimal(price.removeprefix("$")) * 1_000_000))


def _agentcash_usd_amount(price):
    return f"{Decimal(price.removeprefix('$')):.6f}"


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
    asset_schema = {"type": "string"}
    if asset is not None:
        x402_contract["asset"] = asset
        asset_schema["const"] = asset
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
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "x402Version": {
                                                "type": "integer",
                                                "const": 2,
                                            },
                                            "error": {"type": "string"},
                                            "accepts": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "scheme": {
                                                            "type": "string",
                                                            "const": scheme,
                                                        },
                                                        "network": {
                                                            "type": "string",
                                                            "const": network,
                                                        },
                                                        "amount": {
                                                            "type": "string",
                                                            "example": atomic_amount,
                                                        },
                                                        "asset": asset_schema,
                                                        "payTo": {
                                                            "type": "string",
                                                        },
                                                    },
                                                    "required": [
                                                        "scheme",
                                                        "network",
                                                        "amount",
                                                        "asset",
                                                        "payTo",
                                                    ],
                                                },
                                            },
                                            "request_id": {"type": "string"},
                                        },
                                        "required": [
                                            "x402Version",
                                            "accepts",
                                            "request_id",
                                        ],
                                    },
                                },
                            },
                        },
                        "429": _error_response("Rate limit exceeded"),
                        "502": _error_response("Upstream fetch failed"),
                        "503": _error_response("Service is at capacity"),
                        "504": _error_response("Retrieval timed out"),
                    },
                },
            },
        },
    }


def docs_html(urls):
    """Return concise human- and crawler-readable service documentation."""
    safe = {key: escape(value, quote=True) for key, value in urls.items()}
    github = escape(GITHUB_URL, quote=True)
    python_example = escape(PYTHON_EXAMPLE_URL, quote=True)
    typescript_example = escape(TYPESCRIPT_EXAMPLE_URL, quote=True)
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
    <p>Each paid HTTP or MCP execution uses x402 exact payments at a default price of <strong>$0.005</strong>. Discovery, health, metadata, MCP initialize, and MCP tools/list remain free.</p>
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
    <p>Source and deployment documentation: <a href="{github}">{github}</a>.</p>
  </main>
</body>
</html>
"""


def llms_text(urls):
    """Return a compact llms.txt service summary."""
    return f"""# SmartFetch

SmartFetch reads, fetches, scrapes, and extracts public webpages for AI agents. It returns clean text, Markdown, links, and metadata, with browser rendering for JavaScript-heavy websites.

Price: $0.005 per paid HTTP or MCP tool execution using x402 exact payments.

## Endpoints
- Community x402 manifest: {urls['x402']}
- Documentation: {urls['docs']}
- OpenAPI 3.1: {urls['openapi']}
- Metadata: {urls['meta']}
- Remote MCP Streamable HTTP: {urls['mcp']}
- Paid HTTP retrieval: POST {urls['fetch']}

## MCP tools
- fetch_webpage: full SmartFetch result
- webpage_to_markdown: Markdown plus core metadata
- extract_webpage_text: clean text plus core metadata
- render_webpage: forced browser rendering plus the full result

## Paying MCP client examples
- Python: {PYTHON_EXAMPLE_URL}
- TypeScript: {TYPESCRIPT_EXAMPLE_URL}
- Both enforce a $0.005 maximum payment. Running them can spend real Base-mainnet USDC.

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
