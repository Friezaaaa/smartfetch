import io
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from x402.schemas import (
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

from smartfetch import activity, server
from smartfetch.payments import BASE_SEPOLIA, X402Settings


VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
MCP_HEADERS = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}
FREE_SETTINGS = X402Settings(False, None, '$0.005', BASE_SEPOLIA)
PAID_SETTINGS = X402Settings(
    True,
    VALID_ADDRESS,
    '$0.005',
    BASE_SEPOLIA,
)
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme='exact',
    network=BASE_SEPOLIA,
)])


def rpc(client, method, params=None, request_id=1):
    payload = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        payload['params'] = params
    response = client.post('/mcp', headers=MCP_HEADERS, json=payload)
    if response.status_code != 200:
        raise AssertionError(response.text)
    return response.json()


def initialize(client):
    return rpc(client, 'initialize', {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'activity-test', 'version': '1.0'},
    })


def call_tool(client, *, payment=None):
    params = {
        'name': 'fetch_webpage',
        'arguments': {
            'url': 'https://customer-secret.example/account',
            'max_chars': 20000,
            'force_browser': False,
        },
    }
    if payment is not None:
        params['_meta'] = {'x402/payment': payment}
    return rpc(client, 'tools/call', params, request_id=3)


class ActivityEmitterTests(unittest.TestCase):
    def _one_event(self, callback):
        with self.assertLogs('smartfetch.activity', level='INFO') as captured:
            callback()
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertNotIn('\n', message)
        return json.loads(message)

    def test_event_contains_only_allowlisted_operator_fields(self):
        from smartfetch.activity import activity_context, emit_activity

        def emit():
            with activity_context('request-123'):
                emit_activity(
                    'tool_call_attempted',
                    transport='mcp',
                    tool='fetch_webpage',
                    stage='request',
                    outcome='accepted',
                    status=200,
                    duration_ms=7,
                    url='https://private.example/account',
                    content='customer content',
                    payment_signature='secret-signature',
                    wallet_address='0x1111111111111111111111111111111111111111',
                    src_ip='203.0.113.10',
                )

        event = self._one_event(emit)

        self.assertEqual(set(event), {
            'timestamp',
            'event',
            'message',
            'level',
            'request_id',
            'transport',
            'tool',
            'stage',
            'outcome',
            'status',
            'duration_ms',
        })
        self.assertEqual(event['event'], 'tool_call_attempted')
        self.assertEqual(event['message'], 'tool_call_attempted')
        self.assertEqual(event['level'], 'INFO')
        self.assertEqual(event['request_id'], 'request-123')
        self.assertEqual(event['transport'], 'mcp')
        self.assertEqual(event['tool'], 'fetch_webpage')
        self.assertEqual(event['status'], 200)
        self.assertEqual(event['duration_ms'], 7)
        serialized = json.dumps(event)
        for forbidden in (
            'private.example',
            'customer content',
            'secret-signature',
            '0x1111111111111111111111111111111111111111',
            '203.0.113.10',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_caller_cannot_override_timestamp_or_bound_request_id(self):
        from smartfetch.activity import activity_context, emit_activity

        def emit():
            with activity_context('trusted-request'):
                emit_activity(
                    'tools_listed',
                    timestamp='spoofed',
                    request_id='spoofed-request',
                )

        event = self._one_event(emit)

        self.assertNotEqual(event['timestamp'], 'spoofed')
        self.assertTrue(event['timestamp'].endswith('Z'))
        self.assertEqual(event['request_id'], 'trusted-request')

    def test_malformed_optional_values_are_bounded_without_crashing(self):
        from smartfetch.activity import activity_context, emit_activity

        def emit():
            with activity_context('r' * 500):
                emit_activity(
                    'e' * 500,
                    tool=object(),
                    status=object(),
                    duration_ms=-50,
                )

        event = self._one_event(emit)

        self.assertEqual(len(event['event']), 80)
        self.assertEqual(len(event['request_id']), 128)
        self.assertNotIn('tool', event)
        self.assertNotIn('status', event)
        self.assertEqual(event['duration_ms'], 0)

    def test_logging_failure_never_changes_request_behavior(self):
        from unittest.mock import patch

        from smartfetch.activity import emit_activity

        with patch(
            'smartfetch.activity._LOGGER.info',
            side_effect=RuntimeError('logger unavailable'),
        ):
            result = emit_activity('mcp_initialized')

        self.assertIsNone(result)


class MCPActivityIntegrationTests(unittest.TestCase):
    def _events(self, stream):
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    def setUp(self):
        patcher = patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_mcp_requests_emit_discovery_attempt_and_execution_events(self):
        stream = io.StringIO()
        with (
            patch.object(activity._HANDLER, 'stream', stream),
            TestClient(server.create_app(FREE_SETTINGS)) as client,
        ):
            self.assertIn('result', initialize(client))
            self.assertEqual(len(rpc(client, 'tools/list', {}, request_id=2)['result']['tools']), 4)
            result = call_tool(client)

        self.assertTrue(result['result']['isError'])
        events = self._events(stream)
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        self.assertEqual(events[0]['surface'], 'mcp')
        self.assertEqual(events[0]['tool'], 'fetch_webpage')
        self.assertEqual(events[0]['error_code'], 'retrieval_unavailable')
        serialized = stream.getvalue()
        self.assertNotIn('customer-secret.example', serialized)
        self.assertNotIn('private customer content', serialized)

    def test_unpaid_mcp_call_logs_challenge_without_starting_tool(self):
        app = server.create_app(PAID_SETTINGS)
        stream = io.StringIO()
        with (
            patch.object(activity._HANDLER, 'stream', stream),
            TestClient(app) as client,
        ):
            initialize(client)
            response = call_tool(client)

        self.assertTrue(response['result']['isError'])
        events = self._events(stream)
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        self.assertEqual(events[0]['tool'], 'fetch_webpage')
        self.assertNotIn('payment', stream.getvalue())

    def test_unknown_mcp_tool_name_cannot_smuggle_request_data_into_logs(self):
        stream = io.StringIO()
        with TestClient(server.create_app(FREE_SETTINGS)) as client:
            initialize(client)
            with patch.object(activity._HANDLER, 'stream', stream):
                response = rpc(client, 'tools/call', {
                    'name': 'https://customer-secret.example/account',
                    'arguments': {},
                })
        self.assertEqual(response['error']['message'], 'Unknown tool')
        self.assertEqual(self._events(stream), [])
        self.assertNotIn('customer-secret.example', stream.getvalue())

    def test_paid_mcp_call_logs_verified_execution_and_settlement_once(self):
        app = server.create_app(PAID_SETTINGS)
        stream = io.StringIO()
        with (
            patch('x402.mcp.create_payment_wrapper', side_effect=AssertionError('paid wrapper entered')) as wrapper,
            patch.object(activity._HANDLER, 'stream', stream),
            TestClient(app) as client,
        ):
            initialize(client)
            response = call_tool(client, payment={'secretAuthorization': 'never-log-this-payment'})

        self.assertTrue(response['result']['isError'])
        wrapper.assert_not_called()
        events = self._events(stream)
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        serialized = stream.getvalue()
        for forbidden in (
            'customer-secret.example',
            'private customer content',
            'never-log-this-payment',
            VALID_ADDRESS,
            '0xabc123',
            'payment_verified',
            'payment_settled',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_paid_mcp_tool_failure_is_logged_without_exception_data(self):
        app = server.create_app(PAID_SETTINGS)
        stream = io.StringIO()
        with (
            patch.object(activity._HANDLER, 'stream', stream),
            TestClient(app) as client,
        ):
            initialize(client)
            response = call_tool(client, payment={'secretAuthorization': 'never-log-this-payment'})

        self.assertTrue(response['result']['isError'])
        events = self._events(stream)
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        serialized = stream.getvalue() + json.dumps(response)
        self.assertNotIn('customer-secret.example', serialized)
        self.assertNotIn('never-log-this-payment', serialized)
        self.assertNotIn('tool_failed', serialized)


class HTTPActivityIntegrationTests(unittest.TestCase):
    def _events(self, stream):
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    def setUp(self):
        patcher = patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _verified_payment_middleware(**_kwargs):
        async def middleware(request, call_next):
            if request.method == 'POST' and request.url.path == '/fetch':
                request.state.payment_payload = {
                    'secretAuthorization': 'never-log-this-payment',
                }
                response = await call_next(request)
                if response.status_code < 400:
                    response.headers['payment-response'] = (
                        'never-log-this-settlement'
                    )
                return response
            return await call_next(request)

        return middleware

    def test_unpaid_http_fetch_logs_challenge_without_execution(self):
        app = server.create_app(PAID_SETTINGS)
        stream = io.StringIO()
        with (
            patch.object(activity._HANDLER, 'stream', stream),
            TestClient(app) as client,
        ):
            response = client.post('/fetch', json={
                'url': 'https://customer-secret.example/account',
            })

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error_code'], 'retrieval_unavailable')
        events = self._events(stream)
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        self.assertEqual(events[0]['surface'], 'fetch')
        self.assertEqual(events[0]['request_id'], response.headers['x-request-id'])
        self.assertNotIn('customer-secret.example', stream.getvalue())
        self.assertNotIn('payment', stream.getvalue())

    def test_verified_http_fetch_logs_execution_and_settlement(self):
        app = server.create_app(PAID_SETTINGS)
        stream = io.StringIO()
        with (
            patch('x402.http.middleware.fastapi.payment_middleware', side_effect=AssertionError('x402 entered')) as wrapper,
            patch.object(activity._HANDLER, 'stream', stream),
            TestClient(app) as client,
        ):
            response = client.post('/fetch', json={
                'url': 'https://customer-secret.example/account',
            }, headers={'PAYMENT-SIGNATURE': 'never-log-this-payment'})

        wrapper.assert_not_called()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('payment-response', response.headers)
        events = self._events(stream)
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        serialized = stream.getvalue() + response.text
        for forbidden in (
            'customer-secret.example',
            'private customer content',
            'never-log-this-payment',
            'never-log-this-settlement',
            'payment_verified',
            'payment_settled',
            'tool_started',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_failed_http_fetch_logs_failure_without_settlement_or_error_text(self):
        app = server.create_app(PAID_SETTINGS)
        stream = io.StringIO()
        with (
            patch.object(activity._HANDLER, 'stream', stream),
            TestClient(app) as client,
        ):
            response = client.post('/fetch', json={
                'url': 'https://customer-secret.example/account',
            }, headers={'PAYMENT-SIGNATURE': 'never-log-this-payment'})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error_code'], 'retrieval_unavailable')
        events = self._events(stream)
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        serialized = stream.getvalue() + response.text
        self.assertNotIn('/account', serialized)
        self.assertNotIn('never-log-this-payment', serialized)
        self.assertNotIn('tool_failed', serialized)
        self.assertNotIn('payment_settled', serialized)


if __name__ == '__main__':
    unittest.main()
