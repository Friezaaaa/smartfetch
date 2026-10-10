"""Local emergency MCP smoke; performs no payment or network retrieval."""

import socket
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from smartfetch import server
from smartfetch.payments import BASE_SEPOLIA, X402Settings


ADDRESS = '0x1111111111111111111111111111111111111111'
HEADERS = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}


def rpc(client, request_id, method, params=None):
    payload = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        payload['params'] = params
    response = client.post('/mcp', headers=HEADERS, json=payload)
    response.raise_for_status()
    return response.json()


settings = X402Settings(
    True,
    ADDRESS,
    '$0.005',
    BASE_SEPOLIA,
)

with (
    patch('x402.mcp.create_payment_wrapper', side_effect=AssertionError('paid wrapper entered')) as wrapper,
    patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS entered')) as dns,
    TestClient(server.create_app(settings)) as client,
):
    initialized = rpc(client, 1, 'initialize', {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'local-smoke', 'version': '1.0'},
    })
    listed = rpc(client, 2, 'tools/list', {})
    unavailable = {
        name: rpc(client, index, 'tools/call', {
            'name': name, 'arguments': {'url': 'https://example.com/'},
        }) for index, name in enumerate((
            'fetch_webpage', 'webpage_to_markdown', 'extract_webpage_text', 'render_webpage',
        ), start=3)
    }
    absent = rpc(client, 7, 'tools/call', {
        'name': 'search_and_extract', 'arguments': {'query': 'private-canary'},
    })

assert initialized['result']['serverInfo']['name'] == 'SmartFetch'
assert [tool['name'] for tool in listed['result']['tools']] == [
    'fetch_webpage',
    'webpage_to_markdown',
    'extract_webpage_text',
    'render_webpage',
]
for name, result in unavailable.items():
    assert result['result']['isError'] is True
    expected = 'browser_unavailable' if name == 'render_webpage' else 'retrieval_unavailable'
    assert result['result']['structuredContent']['error_code'] == expected
    assert 'accepts' not in str(result)
assert absent['error']['message'] == 'Unknown tool'
wrapper.assert_not_called()
dns.assert_not_called()

print('PASS MCP initialize (free)')
print('PASS MCP tools/list: four SmartFetch tools (free)')
print('PASS MCP four unavailable calls before payment or retrieval')
print('PASS MCP V1.11 tool absent')
