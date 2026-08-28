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

from smartfetch import server
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
            'request_id',
            'transport',
            'tool',
            'stage',
            'outcome',
            'status',
            'duration_ms',
        })
        self.assertEqual(event['event'], 'tool_call_attempted')
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
    def _events(self, captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    def setUp(self):
        patcher = patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_mcp_requests_emit_discovery_attempt_and_execution_events(self):
        fetch_result = {
            'success': True,
            'requested_url': 'https://customer-secret.example/account',
            'final_url': 'https://customer-secret.example/account',
            'status_code': 200,
            'content': 'private customer content',
            'markdown': 'private customer content',
        }
        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                return_value=fetch_result,
            ),
            TestClient(server.create_app(FREE_SETTINGS)) as client,
            self.assertLogs('smartfetch.activity', level='INFO') as captured,
        ):
            initialize(client)
            rpc(client, 'tools/list', {}, request_id=2)
            call_tool(client)

        events = self._events(captured)
        self.assertEqual([event['event'] for event in events], [
            'mcp_initialized',
            'tools_listed',
            'tool_call_attempted',
            'tool_started',
            'tool_completed',
        ])
        call_events = events[2:]
        self.assertEqual(
            {event['request_id'] for event in call_events},
            {call_events[0]['request_id']},
        )
        self.assertTrue(all(
            event['transport'] == 'mcp' for event in events
        ))
        self.assertTrue(all(
            event.get('tool') == 'fetch_webpage' for event in call_events
        ))
        serialized = json.dumps(events)
        self.assertNotIn('customer-secret.example', serialized)
        self.assertNotIn('private customer content', serialized)

    def test_unpaid_mcp_call_logs_challenge_without_starting_tool(self):
        app = server.create_app(PAID_SETTINGS)
        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
            ) as fetch,
            TestClient(app) as client,
        ):
            initialize(client)
            with self.assertLogs(
                'smartfetch.activity',
                level='INFO',
            ) as captured:
                response = call_tool(client)

        fetch.assert_not_awaited()
        self.assertTrue(response['result']['isError'])
        events = self._events(captured)
        self.assertEqual([event['event'] for event in events], [
            'tool_call_attempted',
            'payment_challenged',
        ])
        self.assertTrue(all(
            event['tool'] == 'fetch_webpage' for event in events
        ))

    def test_unknown_mcp_tool_name_cannot_smuggle_request_data_into_logs(self):
        with TestClient(server.create_app(FREE_SETTINGS)) as client:
            initialize(client)
            with self.assertLogs(
                'smartfetch.activity',
                level='INFO',
            ) as captured:
                rpc(client, 'tools/call', {
                    'name': 'https://customer-secret.example/account',
                    'arguments': {},
                })

        events = self._events(captured)
        attempted = next(
            event for event in events
            if event['event'] == 'tool_call_attempted'
        )
        self.assertNotIn('tool', attempted)
        self.assertNotIn('customer-secret.example', json.dumps(events))

    def test_paid_mcp_call_logs_verified_execution_and_settlement_once(self):
        app = server.create_app(PAID_SETTINGS)
        payment_requirement = app.state.smartfetch_mcp.accepts[0]
        resource_server = app.state.smartfetch_mcp.resource_server
        resource_server.find_matching_requirements = Mock(
            return_value=payment_requirement,
        )
        resource_server.verify_payment = AsyncMock(return_value=VerifyResponse(
            isValid=True,
            payer=VALID_ADDRESS,
        ))
        resource_server.settle_payment = AsyncMock(return_value=SettleResponse(
            success=True,
            payer=VALID_ADDRESS,
            transaction='0xabc123',
            network=BASE_SEPOLIA,
        ))
        payment = {
            'x402Version': 2,
            'accepted': payment_requirement.model_dump(
                by_alias=True,
                exclude_none=True,
            ),
            'payload': {'secretAuthorization': 'never-log-this-payment'},
        }
        fetch_result = {
            'success': True,
            'requested_url': 'https://customer-secret.example/account',
            'final_url': 'https://customer-secret.example/account',
            'status_code': 200,
            'content': 'private customer content',
            'markdown': 'private customer content',
        }

        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                return_value=fetch_result,
            ),
            TestClient(app) as client,
        ):
            initialize(client)
            with self.assertLogs(
                'smartfetch.activity',
                level='INFO',
            ) as captured:
                response = call_tool(client, payment=payment)

        self.assertFalse(response['result']['isError'])
        events = self._events(captured)
        self.assertEqual([event['event'] for event in events], [
            'tool_call_attempted',
            'payment_verified',
            'tool_started',
            'tool_completed',
            'payment_settled',
        ])
        self.assertEqual(
            {event['request_id'] for event in events},
            {events[0]['request_id']},
        )
        serialized = json.dumps(events)
        for forbidden in (
            'customer-secret.example',
            'private customer content',
            'never-log-this-payment',
            VALID_ADDRESS,
            '0xabc123',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_paid_mcp_tool_failure_is_logged_without_exception_data(self):
        app = server.create_app(PAID_SETTINGS)
        payment_requirement = app.state.smartfetch_mcp.accepts[0]
        resource_server = app.state.smartfetch_mcp.resource_server
        resource_server.find_matching_requirements = Mock(
            return_value=payment_requirement,
        )
        resource_server.verify_payment = AsyncMock(return_value=VerifyResponse(
            isValid=True,
            payer=VALID_ADDRESS,
        ))
        resource_server.settle_payment = AsyncMock()
        payment = {
            'x402Version': 2,
            'accepted': payment_requirement.model_dump(
                by_alias=True,
                exclude_none=True,
            ),
            'payload': {'secretAuthorization': 'never-log-this-payment'},
        }

        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                side_effect=RuntimeError(
                    'failure at https://customer-secret.example/account'
                ),
            ),
            TestClient(app) as client,
        ):
            initialize(client)
            with self.assertLogs(
                'smartfetch.activity',
                level='INFO',
            ) as captured:
                response = call_tool(client, payment=payment)

        self.assertTrue(response['result']['isError'])
        resource_server.settle_payment.assert_not_awaited()
        events = self._events(captured)
        self.assertEqual([event['event'] for event in events], [
            'tool_call_attempted',
            'payment_verified',
            'tool_started',
            'tool_failed',
        ])
        serialized = json.dumps(events)
        self.assertNotIn('customer-secret.example', serialized)
        self.assertNotIn('never-log-this-payment', serialized)


class HTTPActivityIntegrationTests(unittest.TestCase):
    def _events(self, captured):
        return [json.loads(record.getMessage()) for record in captured.records]

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

        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
            ) as fetch,
            TestClient(app) as client,
            self.assertLogs('smartfetch.activity', level='INFO') as captured,
        ):
            response = client.post('/fetch', json={
                'url': 'https://customer-secret.example/account',
            })

        fetch.assert_not_awaited()
        self.assertEqual(response.status_code, 402)
        events = self._events(captured)
        self.assertEqual([event['event'] for event in events], [
            'tool_call_attempted',
            'payment_challenged',
        ])
        self.assertTrue(all(
            event['transport'] == 'http' for event in events
        ))
        self.assertTrue(all(
            event['tool'] == 'fetch_webpage' for event in events
        ))
        self.assertNotIn('customer-secret.example', json.dumps(events))

    def test_verified_http_fetch_logs_execution_and_settlement(self):
        fetch_result = {
            'success': True,
            'requested_url': 'https://customer-secret.example/account',
            'final_url': 'https://customer-secret.example/account',
            'status_code': 200,
            'content': 'private customer content',
            'markdown': 'private customer content',
        }
        with patch(
            'x402.http.middleware.fastapi.payment_middleware',
            side_effect=self._verified_payment_middleware,
        ):
            app = server.create_app(PAID_SETTINGS)

        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                return_value=fetch_result,
            ) as fetch,
            TestClient(app) as client,
            self.assertLogs('smartfetch.activity', level='INFO') as captured,
        ):
            response = client.post('/fetch', json={
                'url': 'https://customer-secret.example/account',
            })

        fetch.assert_awaited_once()
        self.assertEqual(response.status_code, 200)
        events = self._events(captured)
        self.assertEqual([event['event'] for event in events], [
            'tool_call_attempted',
            'payment_verified',
            'tool_started',
            'tool_completed',
            'payment_settled',
        ])
        self.assertEqual(
            {event['request_id'] for event in events},
            {events[0]['request_id']},
        )
        serialized = json.dumps(events)
        for forbidden in (
            'customer-secret.example',
            'private customer content',
            'never-log-this-payment',
            'never-log-this-settlement',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_failed_http_fetch_logs_failure_without_settlement_or_error_text(self):
        with patch(
            'x402.http.middleware.fastapi.payment_middleware',
            side_effect=self._verified_payment_middleware,
        ):
            app = server.create_app(PAID_SETTINGS)

        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                side_effect=RuntimeError(
                    'failure at https://customer-secret.example/account'
                ),
            ),
            TestClient(app) as client,
            self.assertLogs('smartfetch.activity', level='INFO') as captured,
        ):
            response = client.post('/fetch', json={
                'url': 'https://customer-secret.example/account',
            })

        self.assertEqual(response.status_code, 502)
        events = self._events(captured)
        self.assertEqual([event['event'] for event in events], [
            'tool_call_attempted',
            'payment_verified',
            'tool_started',
            'tool_failed',
        ])
        serialized = json.dumps(events)
        self.assertNotIn('customer-secret.example', serialized)
        self.assertNotIn('never-log-this-payment', serialized)


if __name__ == '__main__':
    unittest.main()
