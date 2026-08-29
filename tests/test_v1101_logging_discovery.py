import io
import json
import logging
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
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
    def setUp(self):
        patcher = patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _payment(app):
        requirement = app.state.smartfetch_mcp.accepts[0]
        return requirement, {
            'x402Version': 2,
            'accepted': requirement.model_dump(
                by_alias=True,
                exclude_none=True,
            ),
            'payload': {'authorization': 'never-log-payment-secret'},
        }

    @staticmethod
    def _events(captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    def test_invalid_mcp_payment_logs_safe_reason_and_never_fetches(self):
        app = server.create_app(PAID_SETTINGS)
        requirement, payment = self._payment(app)
        resource_server = app.state.smartfetch_mcp.resource_server
        resource_server.find_matching_requirements = Mock(
            return_value=requirement,
        )
        resource_server.verify_payment = AsyncMock(return_value=VerifyResponse(
            isValid=False,
            invalidReason='facilitator leaked never-log-invalid-detail',
        ))
        resource_server.settle_payment = AsyncMock()

        with (
            patch('smartfetch.server._run_fetch', new_callable=AsyncMock) as fetch,
            TestClient(app) as client,
        ):
            initialize(client)
            with self.assertLogs('smartfetch.activity', level='INFO') as captured:
                result = call_tool(client, payment)

        self.assertTrue(result['result']['isError'])
        fetch.assert_not_awaited()
        resource_server.settle_payment.assert_not_awaited()
        events = self._events(captured)
        self.assertTrue(events[0]['payment_present'])
        challenged = events[-1]
        self.assertEqual(challenged['event'], 'payment_challenged')
        self.assertEqual(challenged['failure_reason'], 'verification_failed')
        self.assertEqual(challenged['route'], '/mcp')
        self.assertTrue(challenged['payment_present'])
        self.assertEqual(challenged['payment_network'], BASE_SEPOLIA)
        self.assertEqual(challenged['payment_asset'], 'USDC')
        self.assertEqual(challenged['payment_amount'], '$0.005')
        self.assertEqual(challenged['level'], 'INFO')
        serialized = json.dumps(events)
        self.assertNotIn('never-log-invalid-detail', serialized)
        self.assertNotIn('never-log-payment-secret', serialized)

    def test_unpaid_mcp_challenge_marks_payment_absent(self):
        app = server.create_app(PAID_SETTINGS)
        with (
            patch('smartfetch.server._run_fetch', new_callable=AsyncMock) as fetch,
            TestClient(app) as client,
        ):
            initialize(client)
            with self.assertLogs('smartfetch.activity', level='INFO') as captured:
                result = call_tool(client)

        self.assertTrue(result['result']['isError'])
        fetch.assert_not_awaited()
        challenged = self._events(captured)[-1]
        self.assertEqual(challenged['failure_reason'], 'payment_required')
        self.assertFalse(challenged['payment_present'])

    def test_failed_mcp_settlement_logs_error_after_one_fetch(self):
        app = server.create_app(PAID_SETTINGS)
        requirement, payment = self._payment(app)
        resource_server = app.state.smartfetch_mcp.resource_server
        resource_server.find_matching_requirements = Mock(
            return_value=requirement,
        )
        resource_server.verify_payment = AsyncMock(return_value=VerifyResponse(
            isValid=True,
            payer=VALID_ADDRESS,
        ))
        resource_server.settle_payment = AsyncMock(return_value=SettleResponse(
            success=False,
            errorReason='never-log-settlement-detail',
            transaction='',
            network=BASE_SEPOLIA,
        ))
        fetch_result = {
            'success': True,
            'requested_url': 'https://private-customer.example/account',
            'final_url': 'https://private-customer.example/account',
            'status_code': 200,
            'content': 'private content',
            'markdown': 'private content',
        }

        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                return_value=fetch_result,
            ) as fetch,
            TestClient(app) as client,
        ):
            initialize(client)
            with self.assertLogs('smartfetch.activity', level='INFO') as captured:
                result = call_tool(client, payment)

        self.assertTrue(result['result']['isError'])
        fetch.assert_awaited_once()
        resource_server.settle_payment.assert_awaited_once()
        events = self._events(captured)
        settled = events[-1]
        self.assertEqual(settled['event'], 'payment_settled')
        self.assertEqual(settled['outcome'], 'failed')
        self.assertEqual(settled['failure_reason'], 'settlement_failed')
        self.assertEqual(settled['level'], 'ERROR')
        serialized = json.dumps(events)
        self.assertNotIn('never-log-settlement-detail', serialized)
        self.assertNotIn('never-log-payment-secret', serialized)


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

    def test_openapi_describes_active_x402_contract_and_existing_fetch_schema(self):
        payment_requirement = Mock(
            scheme='exact',
            network=BASE_MAINNET,
            asset=BASE_MAINNET_USDC,
            amount='5000',
        )
        document = discovery.openapi_document(
            self.urls,
            self.settings,
            payment_requirement,
        )

        self.assertEqual(document['info']['version'], '1.10.2')
        self.assertEqual(list(document['paths']), ['/fetch'])
        operation = document['paths']['/fetch']['post']
        self.assertEqual(operation['x-x402'], {
            'x402Version': 2,
            'scheme': 'exact',
            'network': BASE_MAINNET,
            'asset': BASE_MAINNET_USDC,
            'assetSymbol': 'USDC',
            'price': '$0.005',
            'amount': '5000',
        })
        body = operation['requestBody']
        self.assertTrue(body['required'])
        self.assertEqual(
            body['content']['application/json']['schema'],
            bazaar.FETCH_INPUT_SCHEMA,
        )
        self.assertEqual(
            body['content']['application/json']['example'],
            bazaar.FETCH_INPUT_EXAMPLE,
        )
        success = operation['responses']['200']['content']['application/json']
        self.assertEqual(success['schema'], bazaar.FETCH_OUTPUT_SCHEMA)
        self.assertEqual(success['example'], bazaar.FETCH_OUTPUT_EXAMPLE)
        requirement = operation['responses']['402']['content'][
            'application/json'
        ]['schema']['properties']['accepts']['items']
        self.assertEqual(
            requirement['properties']['scheme']['const'],
            'exact',
        )
        self.assertEqual(
            requirement['properties']['network']['const'],
            BASE_MAINNET,
        )
        self.assertEqual(
            requirement['properties']['asset']['const'],
            BASE_MAINNET_USDC,
        )
        self.assertEqual(
            requirement['properties']['amount']['example'],
            '5000',
        )

    def test_openapi_asset_matches_generated_mainnet_payment_requirement(self):
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

        generated = app.state.smartfetch_mcp.accepts[0]
        self.assertEqual(generated.asset, BASE_MAINNET_USDC)
        with TestClient(app) as client:
            document = client.get('/openapi.json').json()

        operation = document['paths']['/fetch']['post']
        self.assertEqual(operation['x-x402']['asset'], generated.asset)
        self.assertEqual(operation['x-x402']['assetSymbol'], 'USDC')
        schema = operation['responses']['402']['content'][
            'application/json'
        ]['schema']['properties']['accepts']['items']
        self.assertEqual(
            schema['properties']['asset']['const'],
            generated.asset,
        )
        self.assertNotEqual(
            schema['properties']['asset']['const'],
            'USDC',
        )

    def test_service_version_is_v1102(self):
        self.assertEqual(SERVICE_VERSION, '1.10.2')


if __name__ == '__main__':
    unittest.main()
