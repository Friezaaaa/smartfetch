"""Tests for v1.13 Step 3: FastMCP schemas, parameter descriptions, output validation, and Accept compatibility."""

import unittest
from unittest.mock import AsyncMock
import jsonschema
from starlette.testclient import TestClient

from smartfetch.bazaar import (
    FETCH_OUTPUT_SCHEMA,
    MARKDOWN_OUTPUT_SCHEMA,
    RENDER_OUTPUT_SCHEMA,
    TEXT_OUTPUT_SCHEMA,
)
from smartfetch.mcp_server import create_smartfetch_mcp
from smartfetch.payments import X402Settings
from smartfetch.server import create_app


FREE_SETTINGS = X402Settings(
    enabled=False,
    pay_to=None,
    price="$0.005",
    network="eip155:8453",
)


class TestMCPSchemaAndCompat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_fetch_handler = AsyncMock()
        self.sample_html = "<html><body><h1>Test Page</h1><p>Sample paragraph.</p></body></html>"
        self.mock_fetch_handler.return_value = {
            "success": True,
            "requested_url": "https://example.com/test",
            "final_url": "https://example.com/test",
            "status_code": 200,
            "render_method": "http",
            "elapsed_ms": 42,
            "title": "Test Page",
            "content": "Test Page\nSample paragraph.",
            "markdown": "# Test Page\n\nSample paragraph.",
            "html": self.sample_html,
            "links": [{"href": "https://example.com/about", "text": "About"}],
            "word_count": 4,
            "content_hash": "a" * 64,
            "low_quality": False,
            "truncated": False,
            "original_content_chars": 27,
            "original_markdown_chars": 30,
            "returned_content_chars": 27,
            "returned_markdown_chars": 30,
            "links_returned": 1,
            "max_chars": 20000,
            "request_id": "req-12345678",
            "service_version": "1.13.1",
        }
        self.mcp = create_smartfetch_mcp(FREE_SETTINGS, self.mock_fetch_handler)

    async def test_tools_list_parameter_descriptions(self):
        """All MCP tools expose parameter descriptions in tools/list."""
        tools = await self.mcp.fastmcp.list_tools()
        self.assertEqual(len(tools), 7)
        for tool in tools:
            schema = tool.inputSchema
            self.assertIn("properties", schema, f"Tool {tool.name} missing properties")
            props = schema["properties"]

            # url/urls check
            if tool.name == "batch_fetch_webpages":
                self.assertIn("urls", props, f"Tool {tool.name} missing 'urls' parameter")
                urls_desc = props["urls"].get("description")
                self.assertTrue(urls_desc and len(urls_desc) > 10, f"Tool {tool.name} 'urls' missing clear description")
            else:
                self.assertIn("url", props, f"Tool {tool.name} missing 'url' parameter")
                url_desc = props["url"].get("description")
                self.assertTrue(url_desc and len(url_desc) > 10, f"Tool {tool.name} 'url' missing clear description")

            if tool.name == "screenshot_webpage":
                self.assertIn("viewport", props)
                continue

            if tool.name == "inspect_webpage":
                self.assertIn("render_mode", props)
                self.assertIn("force_browser", props)
                continue

            if tool.name == "batch_fetch_webpages":
                self.assertIn("max_chars_per_url", props)
                continue

            # max_chars check
            self.assertIn("max_chars", props, f"Tool {tool.name} missing 'max_chars' parameter")
            mc_desc = props["max_chars"].get("description")
            self.assertTrue(mc_desc and len(mc_desc) > 10, f"Tool {tool.name} 'max_chars' missing clear description")

            # force_browser check on non-render tools
            if tool.name in ("fetch_webpage", "webpage_to_markdown", "extract_webpage_text"):
                self.assertIn("force_browser", props, f"Tool {tool.name} missing 'force_browser' parameter")
                fb_desc = props["force_browser"].get("description")
                self.assertTrue(fb_desc and len(fb_desc) > 10, f"Tool {tool.name} 'force_browser' missing description")

    async def test_webpage_to_markdown_schema_validation(self):
        """webpage_to_markdown output validates against MARKDOWN_OUTPUT_SCHEMA."""
        tool = self.mcp.fastmcp._tool_manager._tools["webpage_to_markdown"]
        call_res = await tool.fn(url="https://example.com/test", max_chars=20000, force_browser=False)
        self.assertFalse(call_res.isError)
        output = call_res.structuredContent

        jsonschema.validate(instance=output, schema=MARKDOWN_OUTPUT_SCHEMA)
        self.assertIn("markdown", output)
        self.assertNotIn("html", output)
        self.assertEqual(output["title"], "Test Page")

    async def test_extract_webpage_text_schema_validation(self):
        """extract_webpage_text output validates against TEXT_OUTPUT_SCHEMA."""
        tool = self.mcp.fastmcp._tool_manager._tools["extract_webpage_text"]
        call_res = await tool.fn(url="https://example.com/test", max_chars=20000, force_browser=False)
        self.assertFalse(call_res.isError)
        output = call_res.structuredContent

        jsonschema.validate(instance=output, schema=TEXT_OUTPUT_SCHEMA)
        self.assertIn("content", output)
        self.assertNotIn("html", output)
        self.assertEqual(output["title"], "Test Page")

    async def test_render_webpage_schema_validation_and_html(self):
        """render_webpage output validates against RENDER_OUTPUT_SCHEMA and returns real bounded html."""
        tool = self.mcp.fastmcp._tool_manager._tools["render_webpage"]
        call_res = await tool.fn(url="https://example.com/test", max_chars=20000)
        self.assertFalse(call_res.isError)
        output = call_res.structuredContent

        jsonschema.validate(instance=output, schema=RENDER_OUTPUT_SCHEMA)
        self.assertIn("html", output)
        self.assertEqual(output["html"], self.sample_html)
        self.assertEqual(output["render_method"], "browser")
        self.mock_fetch_handler.assert_called_once_with(
            "https://example.com/test", True, 20000, include_html=True
        )


class TestMCPAcceptHeaderNormalization(unittest.TestCase):
    def setUp(self):
        self.app = create_app(FREE_SETTINGS)

    def test_initialize_and_tools_list_accept_variants(self):
        """POST /mcp returns 200 with Accept: application/json, */*, or missing Accept header."""
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-agent", "version": "1.0"},
            },
        }
        tools_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }

        with TestClient(self.app) as client:
            # 1. Accept: application/json
            r1 = client.post("/mcp", json=init_payload, headers={"Accept": "application/json"})
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.json().get("id"), 1)

            r1_tools = client.post("/mcp", json=tools_payload, headers={"Accept": "application/json"})
            self.assertEqual(r1_tools.status_code, 200)
            tools = r1_tools.json().get("result", {}).get("tools", [])
            self.assertEqual(len(tools), 7)

            # 2. Accept: */*
            r2 = client.post("/mcp", json=init_payload, headers={"Accept": "*/*"})
            self.assertEqual(r2.status_code, 200)

            r2_tools = client.post("/mcp", json=tools_payload, headers={"Accept": "*/*"})
            self.assertEqual(r2_tools.status_code, 200)

            # 3. Missing Accept header
            r3 = client.post("/mcp", json=init_payload)
            self.assertEqual(r3.status_code, 200)

            r3_tools = client.post("/mcp", json=tools_payload)
            self.assertEqual(r3_tools.status_code, 200)

    def test_unsupported_accept_header_not_normalized(self):
        """Requests asking for other specific media types (like application/xml) are left untouched."""
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-agent", "version": "1.0"},
            },
        }
        with TestClient(self.app) as client:
            r = client.post("/mcp", json=init_payload, headers={"Accept": "application/xml"})
            # MCP SDK rejects non-matching Accept with 406
            self.assertEqual(r.status_code, 406)

    def test_read_only_mcp_discovery_and_lifecycle_methods(self):
        """Standard MCP methods (prompts/list, resources/list, resources/templates/list, ping, notifications/cancelled) are allowed."""
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-agent", "version": "1.0"},
            },
        }
        with TestClient(self.app) as client:
            client.post("/mcp", json=init_payload)
            client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})

            # prompts/list
            r_prompts = client.post("/mcp", json={"jsonrpc": "2.0", "id": 10, "method": "prompts/list"})
            self.assertEqual(r_prompts.status_code, 200)
            self.assertEqual(r_prompts.json().get("result", {}).get("prompts"), [])

            # resources/list
            r_resources = client.post("/mcp", json={"jsonrpc": "2.0", "id": 11, "method": "resources/list"})
            self.assertEqual(r_resources.status_code, 200)
            self.assertEqual(r_resources.json().get("result", {}).get("resources"), [])

            # resources/templates/list
            r_templates = client.post("/mcp", json={"jsonrpc": "2.0", "id": 12, "method": "resources/templates/list"})
            self.assertEqual(r_templates.status_code, 200)
            self.assertEqual(r_templates.json().get("result", {}).get("resourceTemplates"), [])

            # ping
            r_ping = client.post("/mcp", json={"jsonrpc": "2.0", "id": 13, "method": "ping"})
            self.assertEqual(r_ping.status_code, 200)
            self.assertEqual(r_ping.json().get("result"), {})

            # notifications/cancelled
            r_cancelled = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "test-req-1"}},
            )
            self.assertEqual(r_cancelled.status_code, 202)

    def test_unknown_arbitrary_method_rejected_with_32601(self):
        """Arbitrary unknown methods still return JSON-RPC -32601 Method not available."""
        with TestClient(self.app) as client:
            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": "unknown-1", "method": "foo/bar"})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data.get("id"), "unknown-1")
            self.assertIn("error", data)
            self.assertEqual(data["error"]["code"], -32601)
            self.assertEqual(data["error"]["message"], "Method not available")


if __name__ == "__main__":
    unittest.main()
