import io
import json
import logging
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from x402.http.utils import decode_payment_required_header
from x402.schemas import (
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

from smartfetch import activity, bazaar, discovery, server
from smartfetch.config import SERVICE_VERSION
from smartfetch.payments import BASE_MAINNET, BASE_SEPOLIA, X402Settings


VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
MCP_HEADERS = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
    'User-Agent': 'SmartFetch-MCP-Test/1.0',
}
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
SUPPORTED_MAINNET = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme='exact',
    network=BASE_MAINNET,
)])
BASE_MAINNET_USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'


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
        'clientInfo': {'name': 'v1101-test', 'version': '1.0'},
    })


def call_tool(client, payment=None):
    params = {
        'name': 'fetch_webpage',
        'arguments': {
            'url': 'https://private-customer.example/account',
            'max_chars': 20000,
            'force_browser': False,
        },
    }
    if payment is not None:
        params['_meta'] = {'x402/payment': payment}
    return rpc(client, 'tools/call', params, request_id=3)


class ActivityOutputTests(unittest.TestCase):
    @staticmethod
    def _owned_handler():
        handlers = [
            handler for handler in activity._LOGGER.handlers
            if getattr(handler, '_smartfetch_activity_handler', False)
        ]
        if len(handlers) != 1:
            raise AssertionError(f'expected one owned handler, got {handlers!r}')
        return handlers[0]

    def test_activity_logger_owns_one_plain_stdout_handler(self):
        handler = self._owned_handler()

        self.assertFalse(activity._LOGGER.propagate)
        self.assertIs(handler.stream, sys.stdout)
        self.assertEqual(handler.level, logging.INFO)
        self.assertEqual(handler.formatter._fmt, '%(message)s')

    def test_events_are_one_compact_json_line_with_safe_structured_fields(self):
        handler = self._owned_handler()
        output = io.StringIO()
        previous_stream = handler.setStream(output)
        try:
            with activity.activity_context('request-123'):
                activity.emit_activity(
                    'payment_challenged',
                    transport='http',
                    tool='fetch_webpage',
                    route='/fetch',
                    stage='challenge',
                    outcome='payment_required',
                    status=402,
                    payment_present=False,
                    payment_stage='challenge',
                    payment_network=BASE_MAINNET,
                    payment_asset='USDC',
                    payment_amount='$0.005',
                    failure_reason='payment_required',
                    client_category='python-http',
                    private_key='never-log-private-key',
                    payment_signature='never-log-signature',
                    url='https://private-customer.example/account',
                )
                activity.emit_activity(
                    'tool_failed',
                    transport='http',
                    tool='fetch_webpage',
                    route='/fetch',
                    stage='execution',
                    outcome='failed',
                    status=502,
                    failure_reason='retrieval_failed',
                )
        finally:
            handler.setStream(previous_stream)

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith('{') and line.endswith('}') for line in lines))
        self.assertNotIn('INFO ', output.getvalue())
        self.assertNotIn('activity.py', output.getvalue())
        events = [json.loads(line) for line in lines]
        challenged, failed = events
        self.assertEqual(challenged['message'], 'payment_challenged')
        self.assertEqual(challenged['level'], 'INFO')
        self.assertEqual(challenged['request_id'], 'request-123')
        self.assertEqual(challenged['route'], '/fetch')
        self.assertFalse(challenged['payment_present'])
        self.assertEqual(challenged['payment_stage'], 'challenge')
        self.assertEqual(challenged['payment_network'], BASE_MAINNET)
        self.assertEqual(challenged['payment_asset'], 'USDC')
        self.assertEqual(challenged['payment_amount'], '$0.005')
        self.assertEqual(challenged['failure_reason'], 'payment_required')
        self.assertEqual(challenged['client_category'], 'python-http')
        self.assertEqual(failed['message'], 'tool_failed')
        self.assertEqual(failed['level'], 'ERROR')
        serialized = output.getvalue()
        for forbidden in (
            'never-log-private-key',
            'never-log-signature',
            'private-customer.example',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_expected_client_payment_rejections_stay_info(self):
        with self.assertLogs('smartfetch.activity', level='INFO') as captured:
            activity.emit_activity(
                'payment_challenged',
                outcome='payment_required',
                failure_reason='verification_failed',
            )
        self.assertEqual(captured.records[0].levelno, logging.INFO)
        self.assertEqual(
            json.loads(captured.records[0].getMessage())['level'],
            'INFO',
        )


class PaymentFailureActivityTests(unittest.TestCase):
    @staticmethod
    def _call_and_events(client, payment=None):
        output = io.StringIO()
        previous_stream = activity._HANDLER.setStream(output)
        try:
            result = call_tool(client, payment)
        finally:
            activity._HANDLER.setStream(previous_stream)
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_invalid_mcp_payment_logs_safe_reason_and_never_fetches(self):
        with (
            patch('x402.server.x402ResourceServer.verify_payment',
                  side_effect=AssertionError('verification entered')) as verify,
            patch('x402.server.x402ResourceServer.settle_payment',
                  side_effect=AssertionError('settlement entered')) as settle,
            TestClient(server.create_app(PAID_SETTINGS)) as client,
        ):
            initialize(client)
            result, events = self._call_and_events(
                client, {'authorization': 'never-log-payment-secret'})

        self.assertTrue(result['result']['isError'])
        self.assertEqual(result['result']['structuredContent']['error_code'],
                         'retrieval_unavailable')
        verify.assert_not_called()
        settle.assert_not_called()
        self.assertEqual([item['event'] for item in events], ['unavailable_attempt'])
        self.assertEqual(events[0]['error_code'], 'retrieval_unavailable')
        serialized = json.dumps(events)
        self.assertNotIn('never-log-payment-secret', serialized)
        self.assertNotIn('private-customer.example', serialized)

    def test_unpaid_mcp_call_logs_unavailable_not_payment_challenge(self):
        with TestClient(server.create_app(PAID_SETTINGS)) as client:
            initialize(client)
            result, events = self._call_and_events(client)

        self.assertTrue(result['result']['isError'])
        self.assertEqual([item['event'] for item in events], ['unavailable_attempt'])
        self.assertEqual(events[0]['error_code'], 'retrieval_unavailable')
        self.assertNotIn('payment', json.dumps(events))

    def test_payment_canary_never_causes_settlement_or_success_activity(self):
        with (
            patch('x402.server.x402ResourceServer.settle_payment',
                  side_effect=AssertionError('settlement entered')) as settle,
            TestClient(server.create_app(PAID_SETTINGS)) as client,
        ):
            initialize(client)
            result, events = self._call_and_events(
                client, {'authorization': 'never-log-payment-secret'})

        self.assertTrue(result['result']['isError'])
        settle.assert_not_called()
        self.assertEqual([item['event'] for item in events], ['unavailable_attempt'])
        serialized = json.dumps(events)
        self.assertNotIn('never-log-payment-secret', serialized)
        self.assertNotIn('private-customer.example', serialized)


class DiscoveryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.urls = {
            'base': 'https://agent.example',
            'x402': 'https://agent.example/.well-known/x402',
            'docs': 'https://agent.example/docs',
            'openapi': 'https://agent.example/openapi.json',
            'llms': 'https://agent.example/llms.txt',
            'robots': 'https://agent.example/robots.txt',
            'sitemap': 'https://agent.example/sitemap.xml',
            'meta': 'https://agent.example/meta',
            'fetch': 'https://agent.example/fetch',
            'mcp': 'https://agent.example/mcp',
        }
        self.settings = X402Settings(
            True,
            VALID_ADDRESS,
            '$0.005',
            BASE_MAINNET,
            'organizations/test/apiKeys/test',
            'never-log-cdp-secret',
        )

    def test_manifest_has_no_ambiguous_bare_tools_or_fake_http_routes(self):
        manifest = discovery.x402_manifest(self.urls, self.settings)

        self.assertNotIn('tools', manifest)
        self.assertEqual(manifest['resources'], ['https://agent.example/fetch'])
        self.assertEqual(manifest['endpoints']['mcp'], {
            'url': 'https://agent.example/mcp',
            'transport': 'streamable-http',
        })
        serialized = json.dumps(manifest)
        for tool in discovery.TOOL_NAMES:
            self.assertNotIn(f'https://agent.example/{tool}', serialized)

    def test_openapi_preserves_fetch_input_schema_without_active_x402(self):
        document = discovery.emergency_openapi_document(self.urls)

        self.assertEqual(document['info']['version'], '1.11.1')
        self.assertEqual(list(document['paths']), ['/fetch'])
        operation = document['paths']['/fetch']['post']
        self.assertEqual(operation['x-smartfetch-availability'], 'unavailable')
        self.assertNotIn('x-x402', operation)
        body = operation['requestBody']
        self.assertTrue(body['required'])
        self.assertEqual(
            body['content']['application/json']['schema'],
            bazaar.FETCH_INPUT_SCHEMA,
        )
        self.assertEqual(set(operation['responses']), {'503', '400', '415', '422'})

    def test_public_openapi_exposes_no_actionable_mainnet_payment_requirement(self):
        facilitator = Mock()
        facilitator.get_supported.return_value = SUPPORTED_MAINNET
        settings = X402Settings(
            True,
            VALID_ADDRESS,
            '$0.005',
            BASE_MAINNET,
            'organizations/test/apiKeys/test',
            'test-credential-not-used',
        )

        with patch(
            'smartfetch.payments.create_facilitator',
            return_value=facilitator,
        ):
            app = server.create_app(settings)

        with TestClient(app) as client:
            document = client.get('/openapi.json').json()

        operation = document['paths']['/fetch']['post']
        self.assertEqual(operation['x-smartfetch-availability'], 'unavailable')
        self.assertNotIn('x-x402', operation)
        self.assertNotIn('402', operation['responses'])
        facilitator.get_supported.assert_not_called()

    def test_service_version_is_emergency_v1111(self):
        self.assertEqual(SERVICE_VERSION, '1.11.1')


if __name__ == '__main__':
    unittest.main()
