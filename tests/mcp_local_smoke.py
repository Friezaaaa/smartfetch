"""Local, mocked-payment MCP protocol smoke; performs no network retrieval."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

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


supported = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme='exact',
    network=BASE_SEPOLIA,
)])
settings = X402Settings(
    True,
    ADDRESS,
    '$0.005',
    BASE_SEPOLIA,
)

with (
    patch(
        'x402.http.HTTPFacilitatorClient.get_supported',
        return_value=supported,
    ),
    patch('smartfetch.server._run_fetch', new_callable=AsyncMock) as fetch,
    TestClient(server.create_app(settings)) as client,
):
    initialized = rpc(client, 1, 'initialize', {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'local-smoke', 'version': '1.0'},
    })
    listed = rpc(client, 2, 'tools/list', {})
    challenged = rpc(client, 3, 'tools/call', {
        'name': 'fetch_webpage',
        'arguments': {'url': 'https://example.com/'},
    })

assert initialized['result']['serverInfo']['name'] == 'SmartFetch'
assert [tool['name'] for tool in listed['result']['tools']] == [
    'fetch_webpage',
    'webpage_to_markdown',
    'extract_webpage_text',
    'render_webpage',
]
challenge = challenged['result']['structuredContent']
assert challenged['result']['isError'] is True
assert challenge['error'] == 'Payment Required'
assert challenge['accepts'][0]['scheme'] == 'exact'
assert challenge['accepts'][0]['amount'] == '5000'
assert challenge['accepts'][0]['network'] == BASE_SEPOLIA
fetch.assert_not_awaited()

print('PASS MCP initialize (free)')
print('PASS MCP tools/list: four SmartFetch tools (free)')
print('PASS MCP unpaid x402 challenge (no retrieval)')
