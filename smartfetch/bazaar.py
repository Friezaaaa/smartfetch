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
