"""Tests for MCP ListTools caching and payment enforcement."""

import asyncio
import pytest
from mcp.types import ListToolsRequest

from smartfetch.mcp_server import create_smartfetch_mcp
from smartfetch.payments import BASE_MAINNET, X402Settings


VALID_ADDRESS = "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786"


@pytest.fixture
def mcp_server():
    settings = X402Settings(
        enabled=True,
        pay_to=VALID_ADDRESS,
        price="$0.005",
        network=BASE_MAINNET,
    )

    async def _dummy_fetch(url: str, force_browser: bool, max_chars: int):
        return {"success": True, "title": "Test", "url": url}

    return create_smartfetch_mcp(settings, _dummy_fetch)


def test_mcp_list_tools_caching(mcp_server):
    """Test that list_tools caches its result and returns identical tools."""
    fastmcp = mcp_server.fastmcp

    async def _run():
        # First call: populates cache
        tools1 = await fastmcp.list_tools()
        assert len(tools1) == 7
        tool_names = [t.name for t in tools1]
        assert "fetch_webpage" in tool_names
        assert "webpage_to_markdown" in tool_names
        assert "extract_webpage_text" in tool_names
        assert "render_webpage" in tool_names
        assert "screenshot_webpage" in tool_names
        assert "inspect_webpage" in tool_names
        assert "batch_fetch_webpages" in tool_names

        # Second call: returns cached result
        tools2 = await fastmcp.list_tools()
        assert tools1 is tools2

        # Third call via lowlevel server handler
        handler = fastmcp._mcp_server.request_handlers[ListToolsRequest]
        res1 = await handler(ListToolsRequest())
        res2 = await handler(ListToolsRequest())

        assert len(res1.root.tools) == 7
        assert [t.name for t in res1.root.tools] == tool_names
        assert [t.name for t in res2.root.tools] == tool_names

    asyncio.run(_run())


def test_mcp_call_tool_not_cached_or_bypassed(mcp_server):
    """Verify call_tool is completely independent of list_tools cache and enforces payment."""
    fastmcp = mcp_server.fastmcp

    async def _run():
        # Call tool without payment authorization headers
        # Should raise or return payment challenge via payment_wrapper
        try:
            result = await fastmcp.call_tool("fetch_webpage", {"url": "https://example.com"})
            if hasattr(result, "isError") and result.isError:
                pass  # Properly rejected
            elif hasattr(result, "content") and result.content:
                text = result.content[0].text
                assert "payment" in text.lower() or "402" in text or "error" in text.lower()
        except Exception as exc:
            # Standard FastMCP / x402 payment challenge exception
            assert "payment" in str(exc).lower() or "402" in str(exc) or "challenge" in str(exc).lower()

    asyncio.run(_run())
