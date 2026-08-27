"""Static x402 Bazaar discovery metadata for SmartFetch."""

from copy import deepcopy

from .config import SERVICE_VERSION


FETCH_DESCRIPTION = (
    "Read, fetch, scrape, or extract any public webpage or URL for AI agents. "
    "Returns clean text, Markdown, links, and metadata, with automatic browser "
    "rendering for JavaScript-heavy pages."
)
FETCH_TAGS = [
    "web-reader",
    "web-scraping",
    "markdown",
    "browser",
    "agents",
]
MARKDOWN_DESCRIPTION = (
    "Convert a public webpage or URL into clean Markdown for AI agents, "
    "with core retrieval metadata."
)
TEXT_DESCRIPTION = (
    "Extract clean readable text from a public webpage or URL for AI agents, "
    "with core retrieval metadata."
)
RENDER_DESCRIPTION = (
    "Browser-render a public JavaScript-heavy webpage or URL, then return "
    "clean text, Markdown, links, and metadata."
)
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
            "description": "Public HTTP or HTTPS URL to retrieve.",
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
)


def _input_without_force_browser():
    schema = deepcopy(FETCH_INPUT_SCHEMA)
    schema["properties"].pop("force_browser")
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
RENDER_OUTPUT_EXAMPLE = deepcopy(FETCH_OUTPUT_EXAMPLE)
RENDER_OUTPUT_EXAMPLE["render_method"] = "browser"


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
