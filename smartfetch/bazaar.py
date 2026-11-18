"""Static x402 Bazaar discovery metadata for SmartFetch."""

from copy import deepcopy

from .config import SERVICE_VERSION


FETCH_DESCRIPTION = (
    "Primary webpage and PDF retrieval tool. Use by default to fetch any public "
    "HTTP/HTTPS webpage or PDF and return clean text, Markdown, links, and metadata. "
    "Uses fast HTTP first and automatically falls back to an isolated headless "
    "browser for JavaScript-rendered pages. (Use webpage_to_markdown or "
    "extract_webpage_text for token-saving single-format output, or render_webpage "
    "when you need raw rendered HTML DOM.)"
)
FETCH_TAGS = [
    "web-reader",
    "web-scraping",
    "markdown",
    "browser",
    "agents",
]
MARKDOWN_DESCRIPTION = (
    "Token-efficient converter that returns ONLY clean Markdown and core metadata "
    "from a public webpage or PDF (omits duplicate plain text, links array, and "
    "raw HTML). Use when you only need Markdown."
)
TEXT_DESCRIPTION = (
    "Token-efficient extractor that returns ONLY clean plain text and core metadata "
    "from a public webpage or PDF (omits Markdown formatting, links array, and "
    "raw HTML). Use when you only need plain readable text."
)
RENDER_DESCRIPTION = (
    "Forced headless Chromium browser renderer for JavaScript-heavy SPAs and "
    "dynamic webpages. Always executes Tier 3 isolated browser rendering and "
    "returns the post-JS rendered HTML DOM alongside extracted text, Markdown, "
    "links, and metadata. Do not use for PDFs or static pages (use fetch_webpage instead)."
)
SCREENSHOT_DESCRIPTION = (
    "Browser-based webpage screenshot capture tool. Captures high-resolution JPEG or "
    "PNG screenshots of public webpages using isolated headless Chromium with "
    "configurable desktop/mobile viewport and optional full-page capture."
)
SCREENSHOT_TAGS = [
    "screenshot",
    "webpage-capture",
    "browser",
    "visual-ai",
    "agents",
]
FETCH_INPUT_EXAMPLE = {
    "url": "https://example.com/article",
    "max_chars": 20000,
    "force_browser": False,
}
FETCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "format": "uri",
            "pattern": "^https?://",
            "description": "Public HTTP or HTTPS webpage or PDF URL to retrieve.",
        },
        "max_chars": {
            "type": "integer",
            "minimum": 1000,
            "maximum": 50000,
            "default": 20000,
            "description": "Maximum characters returned for content and Markdown.",
        },
        "force_browser": {
            "type": "boolean",
            "default": False,
            "description": "Start with browser rendering instead of HTTP retrieval.",
        },
    },
    "required": ["url"],
}
FETCH_OUTPUT_EXAMPLE = {
    "success": True,
    "requested_url": "https://example.com/article",
    "final_url": "https://example.com/article",
    "status_code": 200,
    "render_method": "http",
    "elapsed_ms": 54,
    "title": "SmartFetch local API test",
    "content": (
        "SmartFetch local API test\n"
        "This is useful article content for validating the "
        "production API layer. It contains enough text to pass "
        "extraction and verifies that navigation boilerplate is "
        "removed correctly from the result.\n"
        "Second paragraph with a\nuseful link\n."
    ),
    "markdown": (
        "# SmartFetch local API test\n\n"
        "This is useful article content for validating the "
        "production API layer. It contains enough text to pass "
        "extraction and verifies that navigation boilerplate is "
        "removed correctly from the result.\n\n"
        "Second paragraph with a [useful link](/next)."
    ),
    "links": [{
        "href": "https://example.com/next",
        "text": "useful link",
    }],
    "word_count": 40,
    "content_hash": (
        "3ae07fdaa195ad19b590e0bdf032e362"
        "ae7d573330dd978027880b0706dc62e7"
    ),
    "low_quality": False,
    "truncated": False,
    "original_content_chars": 257,
    "original_markdown_chars": 269,
    "returned_content_chars": 257,
    "returned_markdown_chars": 269,
    "links_returned": 1,
    "max_chars": 20000,
    "request_id": "a1b2c3d4e5f60708",
    "service_version": SERVICE_VERSION,
}
FETCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "requested_url": {"type": "string", "format": "uri"},
        "final_url": {"type": "string", "format": "uri"},
        "status_code": {"type": "integer"},
        "render_method": {
            "type": "string",
            "enum": ["http", "browser"],
        },
        "elapsed_ms": {"type": "integer", "minimum": 0},
        "fallback_reason": {"type": "string"},
        "title": {"type": "string"},
        "content": {"type": "string"},
        "markdown": {"type": "string"},
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "href": {"type": "string", "format": "uri"},
                    "text": {"type": "string"},
                },
                "required": ["href", "text"],
            },
        },
        "word_count": {"type": "integer", "minimum": 0},
        "content_hash": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "low_quality": {"type": "boolean"},
        "truncated": {"type": "boolean"},
        "original_content_chars": {
            "type": "integer",
            "minimum": 0,
        },
        "original_markdown_chars": {
            "type": "integer",
            "minimum": 0,
        },
        "returned_content_chars": {
            "type": "integer",
            "minimum": 0,
        },
        "returned_markdown_chars": {
            "type": "integer",
            "minimum": 0,
        },
        "links_returned": {"type": "integer", "minimum": 0},
        "max_chars": {"type": "integer", "minimum": 1000},
        "request_id": {"type": "string"},
        "service_version": {"type": "string"},
        "content_type": {"type": "string"},
        "page_count": {"type": "integer", "minimum": 1},
    },
    "required": [
        "success",
        "requested_url",
        "final_url",
        "status_code",
        "render_method",
        "elapsed_ms",
        "title",
        "content",
        "markdown",
        "links",
        "word_count",
        "content_hash",
        "low_quality",
        "truncated",
        "original_content_chars",
        "original_markdown_chars",
        "returned_content_chars",
        "returned_markdown_chars",
        "links_returned",
        "max_chars",
        "request_id",
        "service_version",
    ],
}

MCP_COMMON_OUTPUT_FIELDS = (
    "success",
    "requested_url",
    "final_url",
    "status_code",
    "render_method",
    "elapsed_ms",
    "fallback_reason",
    "title",
    "truncated",
    "max_chars",
    "request_id",
    "service_version",
    "content_type",
    "page_count",
)


def _input_without_force_browser():
    schema = deepcopy(FETCH_INPUT_SCHEMA)
    schema["properties"].pop("force_browser")
    schema["properties"]["url"]["description"] = (
        "Public HTTP or HTTPS webpage URL for forced browser rendering."
    )
    example = deepcopy(FETCH_INPUT_EXAMPLE)
    example.pop("force_browser")
    return schema, example


def _projected_output(primary_field):
    fields = (*MCP_COMMON_OUTPUT_FIELDS, primary_field)
    properties = {
        field: deepcopy(FETCH_OUTPUT_SCHEMA["properties"][field])
        for field in fields
    }
    required = [
        field
        for field in fields
        if field in FETCH_OUTPUT_SCHEMA["required"]
    ]
    example = {
        field: deepcopy(FETCH_OUTPUT_EXAMPLE[field])
        for field in fields
        if field in FETCH_OUTPUT_EXAMPLE
    }
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }, example


RENDER_INPUT_SCHEMA, RENDER_INPUT_EXAMPLE = _input_without_force_browser()
MARKDOWN_OUTPUT_SCHEMA, MARKDOWN_OUTPUT_EXAMPLE = _projected_output("markdown")
TEXT_OUTPUT_SCHEMA, TEXT_OUTPUT_EXAMPLE = _projected_output("content")

RENDER_OUTPUT_SCHEMA = deepcopy(FETCH_OUTPUT_SCHEMA)
RENDER_OUTPUT_SCHEMA["properties"]["html"] = {"type": "string"}
RENDER_OUTPUT_SCHEMA["required"] = list(FETCH_OUTPUT_SCHEMA["required"]) + ["html"]

RENDER_OUTPUT_EXAMPLE = deepcopy(FETCH_OUTPUT_EXAMPLE)
RENDER_OUTPUT_EXAMPLE["render_method"] = "browser"
RENDER_OUTPUT_EXAMPLE["html"] = (
    "<!DOCTYPE html><html><head><title>SmartFetch local API test</title></head>"
    "<body><h1>SmartFetch local API test</h1>"
    "<p>This is useful article content for validating the production API layer.</p>"
    "<p>Second paragraph with a <a href=\"https://example.com/next\">useful link</a>.</p>"
    "</body></html>"
)


def fetch_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for POST /fetch."""
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=FETCH_INPUT_EXAMPLE,
        input_schema=FETCH_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(
            example=FETCH_OUTPUT_EXAMPLE,
            schema=FETCH_OUTPUT_SCHEMA,
        ),
    )
    # x402 2.20 adds the method during request enrichment, but its middleware
    # validates declarations before that hook runs. This route is statically
    # POST-only, so include the same known method up front as well.
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def mcp_discovery_extension(
    *,
    tool_name,
    description,
    input_schema,
    input_example,
    output_schema,
    output_example,
    transport="streamable-http",
):
    """Build an official x402 v2 Bazaar declaration for one MCP tool."""
    from x402.extensions.bazaar import (
        DeclareMcpDiscoveryConfig,
        OutputConfig,
        declare_mcp_discovery_extension,
    )

    return declare_mcp_discovery_extension(DeclareMcpDiscoveryConfig(
        tool_name=tool_name,
        description=description,
        transport=transport,
        input_schema=input_schema,
        example=input_example,
        output=OutputConfig(
            example=output_example,
            schema=output_schema,
        ),
    ))


def fetch_mcp_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for fetch_webpage."""
    return mcp_discovery_extension(
        tool_name="fetch_webpage",
        description=FETCH_DESCRIPTION,
        input_schema=FETCH_INPUT_SCHEMA,
        input_example=FETCH_INPUT_EXAMPLE,
        output_schema=FETCH_OUTPUT_SCHEMA,
        output_example=FETCH_OUTPUT_EXAMPLE,
    )


SCREENSHOT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "format": "uri",
            "pattern": "^https?://",
            "description": "Public HTTP or HTTPS webpage URL to capture.",
        },
        "viewport": {
            "type": "string",
            "enum": ["desktop", "mobile"],
            "default": "desktop",
            "description": "Device viewport preset (desktop: 1280x800, mobile: 390x844).",
        },
        "full_page": {
            "type": "boolean",
            "default": False,
            "description": "Capture the full scrollable page height up to 4000px.",
        },
        "image_format": {
            "type": "string",
            "enum": ["jpeg", "png"],
            "default": "jpeg",
            "description": "Output image format (jpeg or png).",
        },
    },
    "required": ["url"],
}
SCREENSHOT_INPUT_EXAMPLE = {
    "url": "https://example.com",
    "viewport": "desktop",
    "full_page": False,
    "image_format": "jpeg",
}
SCREENSHOT_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "requested_url": {"type": "string", "format": "uri"},
        "final_url": {"type": "string", "format": "uri"},
        "status_code": {"type": "integer"},
        "render_method": {"type": "string", "enum": ["browser"]},
        "elapsed_ms": {"type": "integer", "minimum": 0},
        "title": {"type": "string"},
        "image_base64": {"type": "string", "description": "Base64-encoded image data."},
        "mime_type": {"type": "string", "enum": ["image/jpeg", "image/png"]},
        "image_format": {"type": "string", "enum": ["jpeg", "png"]},
        "viewport": {"type": "string", "enum": ["desktop", "mobile"]},
        "full_page": {"type": "boolean"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "byte_size": {"type": "integer"},
        "request_id": {"type": "string"},
        "service_version": {"type": "string"},
    },
    "required": [
        "success",
        "requested_url",
        "final_url",
        "status_code",
        "render_method",
        "elapsed_ms",
        "title",
        "image_base64",
        "mime_type",
        "image_format",
        "viewport",
        "full_page",
        "width",
        "height",
        "byte_size",
        "request_id",
        "service_version",
    ],
}
SCREENSHOT_OUTPUT_EXAMPLE = {
    "success": True,
    "requested_url": "https://example.com",
    "final_url": "https://example.com",
    "status_code": 200,
    "render_method": "browser",
    "elapsed_ms": 612,
    "title": "Example Domain",
    "image_base64": "/9j/4AAQSkZJRgABAQEASABIAAD/2wBD...",
    "mime_type": "image/jpeg",
    "image_format": "jpeg",
    "viewport": "desktop",
    "full_page": False,
    "width": 1280,
    "height": 800,
    "byte_size": 45120,
    "request_id": "a1b2c3d4e5f60708",
    "service_version": SERVICE_VERSION,
}


def screenshot_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for POST /screenshot."""
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=SCREENSHOT_INPUT_EXAMPLE,
        input_schema=SCREENSHOT_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(
            example=SCREENSHOT_OUTPUT_EXAMPLE,
            schema=SCREENSHOT_OUTPUT_SCHEMA,
        ),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def screenshot_mcp_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for screenshot_webpage."""
    return mcp_discovery_extension(
        tool_name="screenshot_webpage",
        description=SCREENSHOT_DESCRIPTION,
        input_schema=SCREENSHOT_INPUT_SCHEMA,
        input_example=SCREENSHOT_INPUT_EXAMPLE,
        output_schema=SCREENSHOT_OUTPUT_SCHEMA,
        output_example=SCREENSHOT_OUTPUT_EXAMPLE,
    )


INSPECT_DESCRIPTION = (
    "Structured webpage and site intelligence and SEO inspection tool for any public URL. "
    "Extracts comprehensive metadata, OpenGraph, Twitter cards, JSON-LD schemas, contact details "
    "(emails, phones, social links), tech stack detection, security headers, and document outline."
)
INSPECT_TAGS = [
    "inspection",
    "metadata",
    "seo",
    "open-graph",
    "json-ld",
    "tech-stack",
    "security",
    "contacts",
]
INSPECT_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "format": "uri",
            "pattern": "^https?://",
            "description": "Public HTTP or HTTPS webpage URL to inspect.",
        },
        "render_mode": {
            "type": "string",
            "enum": ["auto", "http_only", "browser"],
            "default": "auto",
            "description": "Rendering strategy: 'auto' (HTTP with browser escalation on SPA shell), 'http_only', or 'browser'.",
        },
        "force_browser": {
            "type": "boolean",
            "default": False,
            "description": "Force browser rendering bypassing HTTP tier.",
        },
        "no_cache": {
            "type": "boolean",
            "default": False,
            "description": "Bypass in-memory result cache.",
        },
    },
    "required": ["url"],
}
INSPECT_INPUT_EXAMPLE = {
    "url": "https://example.com",
    "render_mode": "auto",
    "force_browser": False,
    "no_cache": False,
}
INSPECT_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "requested_url": {"type": "string", "format": "uri"},
        "final_url": {"type": "string", "format": "uri"},
        "status_code": {"type": "integer"},
        "render_method": {"type": "string", "enum": ["http", "browser"]},
        "retrieval_tier": {"type": "string", "enum": ["http", "browser", "cache"]},
        "cache_hit": {"type": "boolean"},
        "elapsed_ms": {"type": "integer", "minimum": 0},
        "request_id": {"type": "string"},
        "service_version": {"type": "string"},
        "metadata": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "canonical_url": {"type": "string"},
                "language": {"type": "string"},
                "charset": {"type": "string"},
                "favicon_url": {"type": "string"},
                "robots_meta": {"type": "string"},
                "open_graph": {"type": "object"},
                "twitter_card": {"type": "object"},
            },
            "required": ["title", "description", "canonical_url", "language", "charset", "favicon_url", "robots_meta", "open_graph", "twitter_card"],
        },
        "json_ld": {
            "type": "array",
            "items": {"type": "object"},
        },
        "contacts": {
            "type": "object",
            "properties": {
                "emails": {"type": "array", "items": {"type": "string"}},
                "phones": {"type": "array", "items": {"type": "string"}},
                "social_links": {"type": "object"},
            },
            "required": ["emails", "phones", "social_links"],
        },
        "tech_stack": {
            "type": "array",
            "items": {"type": "string"},
        },
        "security_headers": {
            "type": "object",
            "properties": {
                "https": {"type": "boolean"},
                "strict_transport_security": {"type": ["string", "null"]},
                "content_security_policy": {"type": ["string", "null"]},
                "x_frame_options": {"type": ["string", "null"]},
                "x_content_type_options": {"type": ["string", "null"]},
                "referrer_policy": {"type": ["string", "null"]},
                "permissions_policy": {"type": ["string", "null"]},
                "server": {"type": ["string", "null"]},
            },
            "required": ["https"],
        },
        "structure": {
            "type": "object",
            "properties": {
                "h1": {"type": "array", "items": {"type": "string"}},
                "h2": {"type": "array", "items": {"type": "string"}},
                "total_links": {"type": "integer"},
                "internal_links": {"type": "integer"},
                "external_links": {"type": "integer"},
                "pdf_links": {"type": "integer"},
                "sitemap_links": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["h1", "h2", "total_links", "internal_links", "external_links", "pdf_links", "sitemap_links"],
        },
    },
    "required": [
        "success",
        "requested_url",
        "final_url",
        "status_code",
        "render_method",
        "retrieval_tier",
        "cache_hit",
        "elapsed_ms",
        "request_id",
        "service_version",
        "metadata",
        "json_ld",
        "contacts",
        "tech_stack",
        "security_headers",
        "structure",
    ],
}
INSPECT_OUTPUT_EXAMPLE = {
    "success": True,
    "requested_url": "https://example.com",
    "final_url": "https://example.com",
    "status_code": 200,
    "render_method": "http",
    "retrieval_tier": "http",
    "cache_hit": False,
    "elapsed_ms": 115,
    "request_id": "a1b2c3d4e5f60708",
    "service_version": SERVICE_VERSION,
    "metadata": {
        "title": "Example Domain",
        "description": "Example Domain for illustrative examples in documents.",
        "canonical_url": "https://example.com",
        "language": "en",
        "charset": "utf-8",
        "favicon_url": "https://example.com/favicon.ico",
        "robots_meta": "index, follow",
        "open_graph": {
            "title": "Example Domain",
            "description": "Illustrative example domain",
            "type": "website",
        },
        "twitter_card": {
            "card": "summary",
            "title": "Example Domain",
        },
    },
    "json_ld": [
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "Example",
            "url": "https://example.com",
        }
    ],
    "contacts": {
        "emails": ["info@example.com"],
        "phones": ["+1-800-555-0199"],
        "social_links": {
            "github": "https://github.com/example",
            "x_twitter": "https://x.com/example",
            "linkedin": None,
            "youtube": None,
            "discord": None,
            "telegram": None,
            "instagram": None,
            "facebook": None,
        },
    },
    "tech_stack": ["Cloudflare", "Tailwind CSS"],
    "security_headers": {
        "https": True,
        "strict_transport_security": "max-age=31536000; includeSubDomains",
        "content_security_policy": "default-src 'self'",
        "x_frame_options": "DENY",
        "x_content_type_options": "nosniff",
        "referrer_policy": "strict-origin-when-cross-origin",
        "permissions_policy": "camera=(), microphone=()",
        "server": "cloudflare",
    },
    "structure": {
        "h1": ["Example Domain"],
        "h2": ["More Information"],
        "total_links": 1,
        "internal_links": 1,
        "external_links": 0,
        "pdf_links": 0,
        "sitemap_links": ["https://example.com/sitemap.xml"],
    },
}


def inspect_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for POST /inspect."""
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=INSPECT_INPUT_EXAMPLE,
        input_schema=INSPECT_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(
            example=INSPECT_OUTPUT_EXAMPLE,
            schema=INSPECT_OUTPUT_SCHEMA,
        ),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def inspect_mcp_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for inspect_webpage."""
    return mcp_discovery_extension(
        tool_name="inspect_webpage",
        description=INSPECT_DESCRIPTION,
        input_schema=INSPECT_INPUT_SCHEMA,
        input_example=INSPECT_INPUT_EXAMPLE,
        output_schema=INSPECT_OUTPUT_SCHEMA,
        output_example=INSPECT_OUTPUT_EXAMPLE,
    )


BATCH_FETCH_DESCRIPTION = (
    "Parallel batch webpage and document retrieval tool. Fetches up to 5 public URLs "
    "concurrently with fair settlement (returns 502 without settling if all fail), "
    "VPS browser concurrency protection, and native Markdown/plain text output."
)
BATCH_FETCH_TAGS = [
    "batch",
    "parallel",
    "web-scraper",
    "markdown",
    "agents",
]
BATCH_FETCH_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "urls": {
            "type": "array",
            "items": {
                "type": "string",
                "format": "uri",
                "pattern": "^https?://",
            },
            "minItems": 1,
            "maxItems": 5,
            "description": "List of 1 to 5 public HTTP/HTTPS URLs to retrieve in parallel.",
        },
        "max_chars_per_url": {
            "type": "integer",
            "minimum": 1000,
            "maximum": 20000,
            "default": 8000,
            "description": "Maximum characters returned per URL.",
        },
        "render_mode": {
            "type": "string",
            "enum": ["auto", "http_only"],
            "default": "auto",
            "description": "Rendering strategy: 'auto' (fast HTTP with browser fallback capped at 2) or 'http_only'.",
        },
        "no_cache": {
            "type": "boolean",
            "default": False,
            "description": "Bypass cache lookup.",
        },
    },
    "required": ["urls"],
}
BATCH_FETCH_INPUT_EXAMPLE = {
    "urls": [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ],
    "max_chars_per_url": 8000,
    "render_mode": "auto",
    "no_cache": False,
}
BATCH_FETCH_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "results": {
            "type": "array",
            "items": {"type": "object"},
        },
        "total_urls": {"type": "integer", "minimum": 1, "maximum": 5},
        "succeeded_count": {"type": "integer", "minimum": 0, "maximum": 5},
        "failed_count": {"type": "integer", "minimum": 0, "maximum": 5},
        "elapsed_ms": {"type": "integer", "minimum": 0},
        "request_id": {"type": "string"},
        "service_version": {"type": "string"},
    },
    "required": [
        "success",
        "results",
        "total_urls",
        "succeeded_count",
        "failed_count",
        "elapsed_ms",
        "request_id",
        "service_version",
    ],
}
BATCH_FETCH_OUTPUT_EXAMPLE = {
    "success": True,
    "results": [
        {
            "success": True,
            "requested_url": "https://example.com/article-1",
            "final_url": "https://example.com/article-1",
            "status_code": 200,
            "render_method": "http",
            "title": "Article 1",
            "content": "Clean plain text content...",
            "markdown": "# Article 1\n\nClean markdown content...",
            "links": [],
            "word_count": 4,
        },
        {
            "success": True,
            "requested_url": "https://example.com/article-2",
            "final_url": "https://example.com/article-2",
            "status_code": 200,
            "render_method": "http",
            "title": "Article 2",
            "content": "Clean plain text content...",
            "markdown": "# Article 2\n\nClean markdown content...",
            "links": [],
            "word_count": 4,
        },
    ],
    "total_urls": 2,
    "succeeded_count": 2,
    "failed_count": 0,
    "elapsed_ms": 140,
    "request_id": "b1c2d3e4f5a6b7c8",
    "service_version": SERVICE_VERSION,
}


def batch_fetch_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for POST /batch-fetch."""
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=BATCH_FETCH_INPUT_EXAMPLE,
        input_schema=BATCH_FETCH_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(
            example=BATCH_FETCH_OUTPUT_EXAMPLE,
            schema=BATCH_FETCH_OUTPUT_SCHEMA,
        ),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def batch_fetch_mcp_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for batch_fetch_webpages."""
    return mcp_discovery_extension(
        tool_name="batch_fetch_webpages",
        description=BATCH_FETCH_DESCRIPTION,
        input_schema=BATCH_FETCH_INPUT_SCHEMA,
        input_example=BATCH_FETCH_INPUT_EXAMPLE,
        output_schema=BATCH_FETCH_OUTPUT_SCHEMA,
        output_example=BATCH_FETCH_OUTPUT_EXAMPLE,
    )


MAP_DESCRIPTION = (
    "Website URL discovery and sitemap mapper. Discovers and maps internal URLs "
    "across a website using robots.txt, XML sitemaps (including nested sitemap "
    "indexes), and HTML links, with optional search filtering and subdomain support."
)
MAP_TAGS = [
    "sitemap",
    "url-mapper",
    "crawler",
    "discovery",
    "agents",
]
MAP_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "format": "uri",
            "pattern": "^https?://",
            "description": "Public HTTP or HTTPS website URL to map.",
        },
        "search": {
            "type": ["string", "null"],
            "default": None,
            "description": "Optional case-insensitive substring filter for discovered URLs (e.g. 'docs' or 'blog').",
        },
        "include_subdomains": {
            "type": "boolean",
            "default": False,
            "description": "Include URLs on subdomains of the base domain.",
        },
        "max_urls": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 200,
            "description": "Maximum number of URLs returned (1 to 500).",
        },
        "no_cache": {
            "type": "boolean",
            "default": False,
            "description": "Bypass cache lookup.",
        },
    },
    "required": ["url"],
}
MAP_INPUT_EXAMPLE = {
    "url": "https://example.com",
    "search": "docs",
    "include_subdomains": False,
    "max_urls": 200,
    "no_cache": False,
}
MAP_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "requested_url": {"type": "string", "format": "uri"},
        "final_url": {"type": "string", "format": "uri"},
        "base_domain": {"type": "string"},
        "urls": {
            "type": "array",
            "items": {"type": "string", "format": "uri"},
        },
        "total_discovered": {"type": "integer", "minimum": 0},
        "returned_count": {"type": "integer", "minimum": 0},
        "max_urls": {"type": "integer", "minimum": 1, "maximum": 500},
        "include_subdomains": {"type": "boolean"},
        "search": {"type": ["string", "null"]},
        "sources": {
            "type": "object",
            "properties": {
                "sitemap_urls_found": {"type": "integer"},
                "page_links_found": {"type": "integer"},
                "sitemaps_checked": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["sitemap_urls_found", "page_links_found", "sitemaps_checked"],
        },
        "render_method": {"type": "string", "enum": ["http", "browser"]},
        "cache_hit": {"type": "boolean"},
        "elapsed_ms": {"type": "integer", "minimum": 0},
        "request_id": {"type": "string"},
        "service_version": {"type": "string"},
    },
    "required": [
        "success",
        "requested_url",
        "final_url",
        "base_domain",
        "urls",
        "total_discovered",
        "returned_count",
        "max_urls",
        "include_subdomains",
        "search",
        "sources",
        "render_method",
        "cache_hit",
        "elapsed_ms",
        "request_id",
        "service_version",
    ],
}
MAP_OUTPUT_EXAMPLE = {
    "success": True,
    "requested_url": "https://example.com",
    "final_url": "https://example.com/",
    "base_domain": "example.com",
    "urls": [
        "https://example.com/docs",
        "https://example.com/docs/getting-started",
        "https://example.com/docs/api",
    ],
    "total_discovered": 3,
    "returned_count": 3,
    "max_urls": 200,
    "include_subdomains": False,
    "search": "docs",
    "sources": {
        "sitemap_urls_found": 2,
        "page_links_found": 5,
        "sitemaps_checked": ["https://example.com/sitemap.xml"],
    },
    "render_method": "http",
    "cache_hit": False,
    "elapsed_ms": 95,
    "request_id": "c1d2e3f4a5b60708",
    "service_version": SERVICE_VERSION,
}


def map_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for POST /map."""
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=MAP_INPUT_EXAMPLE,
        input_schema=MAP_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(
            example=MAP_OUTPUT_EXAMPLE,
            schema=MAP_OUTPUT_SCHEMA,
        ),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def map_mcp_discovery_extension():
    """Build the official x402 v2 Bazaar declaration for map_website."""
    return mcp_discovery_extension(
        tool_name="map_website",
        description=MAP_DESCRIPTION,
        input_schema=MAP_INPUT_SCHEMA,
        input_example=MAP_INPUT_EXAMPLE,
        output_schema=MAP_OUTPUT_SCHEMA,
        output_example=MAP_OUTPUT_EXAMPLE,
    )


SEARCH_DESCRIPTION = (
    "Live web, news, and places search powered by Serper (with Exa fallback for web). "
    "Returns structured titles, URLs, snippets, and metadata."
)
SEARCH_TAGS = ["search", "web-search", "serper", "exa", "agents"]
SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "Search query string."},
        "provider": {"type": "string", "enum": ["auto", "serper", "exa"], "default": "auto"},
        "search_type": {"type": "string", "enum": ["web", "news", "places"], "default": "web"},
        "num_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 10},
        "country": {"type": "string", "default": "us"},
        "language": {"type": "string", "default": "en"},
        "no_cache": {"type": "boolean", "default": False},
    },
    "required": ["query"],
}
SEARCH_INPUT_EXAMPLE = {
    "query": "x402 protocol micropayments",
    "provider": "auto",
    "search_type": "web",
    "num_results": 5,
}
SEARCH_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "query": {"type": "string"},
        "provider_used": {"type": "string"},
        "search_type": {"type": "string"},
        "num_results": {"type": "integer"},
        "results": {"type": "array", "items": {"type": "object"}},
        "cached": {"type": "boolean"},
        "elapsed_ms": {"type": "integer"},
        "service_version": {"type": "string"},
    },
    "required": ["success", "query", "provider_used", "search_type", "num_results", "results", "cached", "elapsed_ms", "service_version"],
}
SEARCH_OUTPUT_EXAMPLE = {
    "success": True,
    "query": "x402 protocol micropayments",
    "provider_used": "serper",
    "search_type": "web",
    "num_results": 1,
    "results": [{"position": 1, "title": "x402", "url": "https://x402.org", "snippet": "Internet-native payments", "published_date": ""}],
    "cached": False,
    "elapsed_ms": 180,
    "service_version": SERVICE_VERSION,
}


def search_discovery_extension():
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=SEARCH_INPUT_EXAMPLE,
        input_schema=SEARCH_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(example=SEARCH_OUTPUT_EXAMPLE, schema=SEARCH_OUTPUT_SCHEMA),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def search_mcp_discovery_extension():
    return mcp_discovery_extension(
        tool_name="search_web",
        description=SEARCH_DESCRIPTION,
        input_schema=SEARCH_INPUT_SCHEMA,
        input_example=SEARCH_INPUT_EXAMPLE,
        output_schema=SEARCH_OUTPUT_SCHEMA,
        output_example=SEARCH_OUTPUT_EXAMPLE,
    )


SEARCH_AND_FETCH_DESCRIPTION = (
    "Search the web and automatically fetch clean Markdown content for the top matching pages in one call."
)
SEARCH_AND_FETCH_TAGS = ["search", "rag", "markdown", "web-scraping", "agents"]
SEARCH_AND_FETCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "Search query string."},
        "num_results": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
        "max_chars_per_page": {"type": "integer", "minimum": 1000, "maximum": 15000, "default": 6000},
        "provider": {"type": "string", "enum": ["auto", "serper", "exa"], "default": "auto"},
        "render_mode": {"type": "string", "enum": ["auto", "http_only"], "default": "auto"},
        "no_cache": {"type": "boolean", "default": False},
    },
    "required": ["query"],
}
SEARCH_AND_FETCH_INPUT_EXAMPLE = {
    "query": "FastAPI x402 middleware",
    "num_results": 3,
    "max_chars_per_page": 6000,
}
SEARCH_AND_FETCH_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "query": {"type": "string"},
        "provider_used": {"type": "string"},
        "requested_count": {"type": "integer"},
        "succeeded_count": {"type": "integer"},
        "failed_count": {"type": "integer"},
        "results": {"type": "array", "items": {"type": "object"}},
        "elapsed_ms": {"type": "integer"},
        "service_version": {"type": "string"},
    },
    "required": ["success", "query", "provider_used", "requested_count", "succeeded_count", "failed_count", "results", "elapsed_ms", "service_version"],
}
SEARCH_AND_FETCH_OUTPUT_EXAMPLE = {
    "success": True,
    "query": "FastAPI x402 middleware",
    "provider_used": "serper",
    "requested_count": 1,
    "succeeded_count": 1,
    "failed_count": 0,
    "results": [{"url": "https://x402.org", "success": True, "title": "x402", "markdown": "# x402"}],
    "elapsed_ms": 620,
    "service_version": SERVICE_VERSION,
}


def search_and_fetch_discovery_extension():
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=SEARCH_AND_FETCH_INPUT_EXAMPLE,
        input_schema=SEARCH_AND_FETCH_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(example=SEARCH_AND_FETCH_OUTPUT_EXAMPLE, schema=SEARCH_AND_FETCH_OUTPUT_SCHEMA),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def search_and_fetch_mcp_discovery_extension():
    return mcp_discovery_extension(
        tool_name="search_and_fetch",
        description=SEARCH_AND_FETCH_DESCRIPTION,
        input_schema=SEARCH_AND_FETCH_INPUT_SCHEMA,
        input_example=SEARCH_AND_FETCH_INPUT_EXAMPLE,
        output_schema=SEARCH_AND_FETCH_OUTPUT_SCHEMA,
        output_example=SEARCH_AND_FETCH_OUTPUT_EXAMPLE,
    )


LEADS_DESCRIPTION = (
    "B2B Enriched Lead Generator. Searches Google Maps or Web via Serper and enriches each company website "
    "with emails, phones, social links, tech stack, and domain/MX intelligence."
)
LEADS_TAGS = ["leads", "b2b", "enrichment", "prospecting", "agents"]
LEADS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "Business/niche search query (e.g. 'dental clinics austin tx')."},
        "source": {"type": "string", "enum": ["maps", "web"], "default": "maps"},
        "max_leads": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
        "deep": {"type": "boolean", "default": True},
        "country": {"type": "string", "default": "us"},
        "language": {"type": "string", "default": "en"},
        "no_cache": {"type": "boolean", "default": False},
    },
    "required": ["query"],
}
LEADS_INPUT_EXAMPLE = {
    "query": "dental clinics austin tx",
    "source": "maps",
    "max_leads": 5,
    "deep": True,
}
LEADS_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "query": {"type": "string"},
        "source": {"type": "string"},
        "total_leads": {"type": "integer"},
        "inspected_count": {"type": "integer"},
        "leads": {"type": "array", "items": {"type": "object"}},
        "cached": {"type": "boolean"},
        "elapsed_ms": {"type": "integer"},
        "service_version": {"type": "string"},
    },
    "required": ["success", "query", "source", "total_leads", "inspected_count", "leads", "cached", "elapsed_ms", "service_version"],
}
LEADS_OUTPUT_EXAMPLE = {
    "success": True,
    "query": "dental clinics austin tx",
    "source": "maps",
    "total_leads": 1,
    "inspected_count": 1,
    "leads": [{"business_name": "Austin Dental", "website": "https://example.com", "inspected": True, "emails": ["info@example.com"]}],
    "cached": False,
    "elapsed_ms": 950,
    "service_version": SERVICE_VERSION,
}


def leads_discovery_extension():
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=LEADS_INPUT_EXAMPLE,
        input_schema=LEADS_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(example=LEADS_OUTPUT_EXAMPLE, schema=LEADS_OUTPUT_SCHEMA),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def leads_mcp_discovery_extension():
    return mcp_discovery_extension(
        tool_name="search_leads",
        description=LEADS_DESCRIPTION,
        input_schema=LEADS_INPUT_SCHEMA,
        input_example=LEADS_INPUT_EXAMPLE,
        output_schema=LEADS_OUTPUT_SCHEMA,
        output_example=LEADS_OUTPUT_EXAMPLE,
    )


X_SEARCH_DESCRIPTION = (
    "Live X/Twitter post search returning normalized posts, engagement metrics, author details, and pagination cursor."
)
X_SEARCH_TAGS = ["twitter", "x", "social-search", "news", "agents"]
X_SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "X/Twitter advanced search query."},
        "query_type": {"type": "string", "enum": ["Latest", "Top"], "default": "Latest"},
        "max_tweets": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        "cursor": {"type": ["string", "null"], "default": None},
        "no_cache": {"type": "boolean", "default": False},
    },
    "required": ["query"],
}
X_SEARCH_INPUT_EXAMPLE = {
    "query": "x402 OR smartfetch",
    "query_type": "Latest",
    "max_tweets": 10,
}
X_SEARCH_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "query": {"type": "string"},
        "query_type": {"type": "string"},
        "count": {"type": "integer"},
        "tweets": {"type": "array", "items": {"type": "object"}},
        "next_cursor": {"type": ["string", "null"]},
        "has_next_page": {"type": "boolean"},
        "cached": {"type": "boolean"},
        "elapsed_ms": {"type": "integer"},
        "service_version": {"type": "string"},
    },
    "required": ["success", "query", "query_type", "count", "tweets", "has_next_page", "cached", "elapsed_ms", "service_version"],
}
X_SEARCH_OUTPUT_EXAMPLE = {
    "success": True,
    "query": "x402",
    "query_type": "Latest",
    "count": 1,
    "tweets": [{"tweet_id": "123", "url": "https://x.com/i/status/123", "text": "x402 live", "author_username": "dev"}],
    "next_cursor": None,
    "has_next_page": False,
    "cached": False,
    "elapsed_ms": 240,
    "service_version": SERVICE_VERSION,
}


def x_search_discovery_extension():
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

    extension = declare_discovery_extension(
        input=X_SEARCH_INPUT_EXAMPLE,
        input_schema=X_SEARCH_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(example=X_SEARCH_OUTPUT_EXAMPLE, schema=X_SEARCH_OUTPUT_SCHEMA),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def x_search_mcp_discovery_extension():
    return mcp_discovery_extension(
        tool_name="search_x_posts",
        description=X_SEARCH_DESCRIPTION,
        input_schema=X_SEARCH_INPUT_SCHEMA,
        input_example=X_SEARCH_INPUT_EXAMPLE,
        output_schema=X_SEARCH_OUTPUT_SCHEMA,
        output_example=X_SEARCH_OUTPUT_EXAMPLE,
    )

