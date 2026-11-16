"""Tests for v1.13 Phase 4: MCP Tool Migration and Delegation."""

import unittest
from unittest.mock import AsyncMock

from smartfetch.mcp_server import create_smartfetch_mcp
from smartfetch.payments import X402Settings


class TestMCPToolMigration(unittest.IsolatedAsyncioTestCase):
    """Verifies that fetch_webpage is the flagship tool and legacy tools delegate to it."""

    def setUp(self):
        self.settings = X402Settings(
            enabled=False,
            pay_to=None,
            price="$0.005",
            network="eip155:84532",
        )
        self.mock_fetch_handler = AsyncMock()
        self.mock_fetch_handler.return_value = {
            "success": True,
            "requested_url": "https://example.com/blog",
            "final_url": "https://example.com/blog",
            "status_code": 200,
            "render_method": "http",
            "elapsed_ms": 120,
            "title": "Blog Post",
            "content": "Clean plain text content.",
            "markdown": "# Blog Post\n\nClean plain text content.",
            "html": "<html><body><h1>Blog Post</h1><p>Clean plain text content.</p></body></html>",
        }
        self.mcp = create_smartfetch_mcp(self.settings, self.mock_fetch_handler)

    async def test_flagship_fetch_webpage_execution(self):
        tool = self.mcp.fastmcp._tool_manager._tools["fetch_webpage"]
        call_result = await tool.fn(url="https://example.com/blog", max_chars=10000, force_browser=False)
        self.assertFalse(call_result.isError)
        self.assertTrue(call_result.structuredContent["success"])
        self.assertEqual(call_result.structuredContent["title"], "Blog Post")
        self.mock_fetch_handler.assert_called_once_with(
            "https://example.com/blog", False, 10000
        )

    async def test_webpage_to_markdown_delegates_to_fetch_webpage(self):
        tool = self.mcp.fastmcp._tool_manager._tools["webpage_to_markdown"]
        call_result = await tool.fn(url="https://example.com/blog", max_chars=8000, force_browser=False)
        self.assertFalse(call_result.isError)
        self.assertTrue(call_result.structuredContent["success"])
        self.assertIn("# Blog Post", call_result.structuredContent["markdown"])
        self.mock_fetch_handler.assert_called_once_with(
            "https://example.com/blog", False, 8000
        )

    async def test_extract_webpage_text_delegates_to_fetch_webpage(self):
        tool = self.mcp.fastmcp._tool_manager._tools["extract_webpage_text"]
        call_result = await tool.fn(url="https://example.com/blog", max_chars=5000, force_browser=False)
        self.assertFalse(call_result.isError)
        self.assertTrue(call_result.structuredContent["success"])
        self.assertEqual(call_result.structuredContent["content"], "Clean plain text content.")
        self.mock_fetch_handler.assert_called_once_with(
            "https://example.com/blog", False, 5000
        )

    async def test_render_webpage_delegates_with_forced_browser(self):
        tool = self.mcp.fastmcp._tool_manager._tools["render_webpage"]
        call_result = await tool.fn(url="https://example.com/blog", max_chars=15000)
        self.assertFalse(call_result.isError)
        self.assertTrue(call_result.structuredContent["success"])
        self.assertIn("<html>", call_result.structuredContent["html"])
        # render_webpage enforces force_browser=True and include_html=True
        self.mock_fetch_handler.assert_called_once_with(
            "https://example.com/blog", True, 15000, include_html=True
        )

    def test_all_four_tools_registered_in_fastmcp(self):
        tools = self.mcp.fastmcp._tool_manager._tools
        self.assertIn("fetch_webpage", tools)
        self.assertIn("webpage_to_markdown", tools)
        self.assertIn("extract_webpage_text", tools)
        self.assertIn("render_webpage", tools)


if __name__ == "__main__":
    unittest.main()
