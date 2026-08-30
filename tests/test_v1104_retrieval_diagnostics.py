import io
import json
import logging
import socket
import unittest
from unittest.mock import AsyncMock, Mock, patch

import requests
from fastapi.testclient import TestClient

from smartfetch import activity, browser_fetch as browser_module, core, server
from smartfetch.diagnostics import (
    RetrievalDiagnostics,
    RetrievalFailure,
    diagnostics_for_exception,
    normalize_target_host,
)
from smartfetch.http_fetch import _HTTPFetchResult, http_fetch
from smartfetch.payments import BASE_SEPOLIA, X402Settings


FREE_SETTINGS = X402Settings(False, None, '$0.005', BASE_SEPOLIA)
CANARY_USER = 'CANARY_USER_V1104'
CANARY_PASSWORD = 'CANARY_PASSWORD_V1104'
CANARY_PATH = 'CANARY_PATH_V1104'
CANARY_QUERY = 'CANARY_QUERY_V1104'
PRIVATE_URL = (
    f'https://{CANARY_USER}:{CANARY_PASSWORD}@Mixed.Example.COM:8443/'
    f'{CANARY_PATH}?api_key={CANARY_QUERY}#fragment'
)


def response(status_code=200, body=b'<html>ok</html>'):
    result = requests.Response()
    result.status_code = status_code
    result.headers = {'Content-Type': 'text/html; charset=utf-8'}
    result.encoding = 'utf-8'
    result._content = body
    result._content_consumed = True
    return result


class TargetHostNormalizationTests(unittest.TestCase):
    def test_normalizes_hostname_without_url_secrets(self):
        self.assertEqual(normalize_target_host(PRIVATE_URL), 'mixed.example.com')

    def test_ip_literals_are_categorical_and_hosts_are_bounded(self):
        self.assertEqual(normalize_target_host('https://192.0.2.10/a'), 'ip-literal')
        self.assertEqual(normalize_target_host('https://[2001:db8::1]/a'), 'ip-literal')
        long_host = 'a' * 250 + '.example.com'
        normalized = normalize_target_host(f'https://{long_host}/')
        self.assertLessEqual(len(normalized), 253)
        self.assertNotIn('/', normalized)

    def test_malformed_targets_are_safe(self):
        self.assertEqual(
            normalize_target_host(f'not-a-url/{CANARY_QUERY}'),
            'unknown',
        )


class HTTPBoundaryDiagnosticTests(unittest.TestCase):
    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_retry_timeout_is_typed_without_exception_text(self, get, _validate):
        get.side_effect = [
            requests.Timeout('first ' + CANARY_QUERY),
            requests.Timeout('second ' + CANARY_PASSWORD),
        ]

        with self.assertRaises(requests.Timeout) as raised:
            http_fetch(PRIVATE_URL)

        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertEqual(diagnostic.strategy, 'http')
        self.assertEqual(diagnostic.phase, 'connect')
        self.assertEqual(diagnostic.failure_code, 'timeout')
        self.assertTrue(diagnostic.http_attempted)
        self.assertTrue(diagnostic.http_retry_attempted)
        self.assertFalse(diagnostic.browser_attempted)

    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_dns_and_tls_failures_are_classified_by_type(self, get, _validate):
        dns = requests.ConnectionError('do not parse ' + CANARY_QUERY)
        dns.__cause__ = socket.gaierror(-2, 'canary dns detail')
        cases = (
            (dns, 'dns', 'dns'),
            (requests.exceptions.SSLError('canary tls detail'), 'tls', 'tls'),
        )
        for failure, phase, failure_code in cases:
            with self.subTest(failure_code=failure_code):
                get.side_effect = [failure, failure]
                with self.assertRaises(type(failure)) as raised:
                    http_fetch(PRIVATE_URL)
                diagnostic = diagnostics_for_exception(raised.exception)
                self.assertEqual(diagnostic.phase, phase)
                self.assertEqual(diagnostic.failure_code, failure_code)
                self.assertTrue(diagnostic.http_retry_attempted)
                get.reset_mock()

    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_transient_upstream_status_records_retry_and_status(self, get, _validate):
        get.side_effect = [response(503), response(504)]

        with self.assertRaisesRegex(RuntimeError, 'HTTP status 504') as raised:
            http_fetch(PRIVATE_URL)

        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertEqual(diagnostic.phase, 'response')
        self.assertEqual(diagnostic.failure_code, 'upstream_status')
        self.assertEqual(diagnostic.upstream_status, 504)
        self.assertTrue(diagnostic.http_retry_attempted)

    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_blocked_and_invalid_responses_are_typed(self, get, _validate):
        cases = (
            (response(403), 'blocked_response', 403),
            (response(200), 'invalid_content', None),
        )
        cases[1][0].headers = {'Content-Type': 'application/octet-stream'}
        for result, failure_code, upstream_status in cases:
            with self.subTest(failure_code=failure_code):
                get.return_value = result
                with self.assertRaises(RuntimeError) as raised:
                    http_fetch(PRIVATE_URL)
                diagnostic = diagnostics_for_exception(raised.exception)
                self.assertEqual(diagnostic.failure_code, failure_code)
                self.assertEqual(diagnostic.upstream_status, upstream_status)
                get.reset_mock()


class BrowserBoundaryDiagnosticTests(unittest.TestCase):
    @patch(
        'smartfetch.browser_fetch.validate_public_url',
        side_effect=ValueError('blocked ' + CANARY_QUERY),
    )
    def test_browser_validation_failure_is_policy_rejection(self, _validate):
        with self.assertRaises(ValueError) as raised:
            browser_module.browser_fetch(PRIVATE_URL)

        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertEqual(diagnostic.strategy, 'browser')
        self.assertEqual(diagnostic.phase, 'validate')
        self.assertEqual(diagnostic.failure_code, 'policy_rejection')
        self.assertFalse(diagnostic.browser_attempted)

    @patch('smartfetch.browser_fetch._cleanup_profile')
    @patch('smartfetch.browser_fetch._stop_process_tree')
    @patch('smartfetch.browser_fetch._cdp_call')
    @patch('smartfetch.browser_fetch.websocket.create_connection')
    @patch('smartfetch.browser_fetch.requests.Session')
    @patch('smartfetch.browser_fetch.subprocess.Popen')
    @patch('smartfetch.browser_fetch.tempfile.mkdtemp', return_value='profile')
    @patch('smartfetch.browser_fetch._free_port', return_value=9222)
    @patch('smartfetch.browser_fetch.shutil.which', return_value=None)
    @patch('smartfetch.browser_fetch._chromium_path', return_value='chromium')
    def test_navigation_failure_is_typed_at_browser_boundary(
        self,
        _chromium,
        _which,
        _port,
        _profile,
        popen,
        session_class,
        create_connection,
        cdp_call,
        _stop,
        _cleanup,
    ):
        process = popen.return_value
        process.poll.return_value = None
        session = session_class.return_value
        session.get.return_value = Mock(ok=True)
        session.get.return_value.json.return_value = {'ready': True}
        session.put.return_value.json.return_value = {
            'webSocketDebuggerUrl': 'ws://127.0.0.1/devtools',
        }
        session.put.return_value.raise_for_status.return_value = None
        create_connection.return_value = Mock()
        cdp_call.side_effect = [
            {},
            {},
            RuntimeError('navigation ' + CANARY_QUERY),
        ]

        with self.assertRaises(RuntimeError) as raised:
            browser_module._browser_fetch_locked(PRIVATE_URL)

        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertEqual(diagnostic.strategy, 'browser')
        self.assertEqual(diagnostic.phase, 'browser_navigate')
        self.assertEqual(diagnostic.failure_code, 'browser_failure')
        self.assertTrue(diagnostic.browser_attempted)


class RetrievalAggregationTests(unittest.TestCase):
    def test_http_then_browser_failure_reports_fallback_flags(self):
        http_diagnostic = RetrievalDiagnostics(
            target_host='mixed.example.com',
            strategy='http',
            phase='connect',
            failure_code='timeout',
            http_attempted=True,
            http_retry_attempted=True,
            browser_attempted=False,
            fallback_attempted=False,
        )
        browser_diagnostic = RetrievalDiagnostics(
            target_host='mixed.example.com',
            strategy='browser',
            phase='browser_navigate',
            failure_code='browser_failure',
            http_attempted=False,
            http_retry_attempted=False,
            browser_attempted=True,
            fallback_attempted=False,
        )
        http_error = requests.Timeout('http ' + CANARY_QUERY)
        http_error.retrieval_diagnostics = http_diagnostic
        browser_error = RuntimeError('browser ' + CANARY_PASSWORD)
        browser_error.retrieval_diagnostics = browser_diagnostic

        with (
            patch('smartfetch.core.http_fetch', side_effect=http_error),
            patch('smartfetch.core.browser_fetch', side_effect=browser_error),
            self.assertRaises(RetrievalFailure) as raised,
        ):
            core.smart_fetch(PRIVATE_URL)

        diagnostic = raised.exception.retrieval_diagnostics
        self.assertEqual(diagnostic.strategy, 'browser')
        self.assertEqual(diagnostic.phase, 'browser_navigate')
        self.assertEqual(diagnostic.failure_code, 'browser_failure')
        self.assertTrue(diagnostic.http_attempted)
        self.assertTrue(diagnostic.http_retry_attempted)
        self.assertTrue(diagnostic.browser_attempted)
        self.assertTrue(diagnostic.fallback_attempted)
        self.assertIn('SmartFetch failed. HTTP:', str(raised.exception))

    def test_successful_http_retry_is_retained_when_browser_fallback_fails(self):
        page = _HTTPFetchResult({
            'html': '<html>thin</html>',
            'final_url': 'https://mixed.example.com/',
            'status_code': 200,
        }, retry_attempted=True)
        browser_error = RuntimeError('browser failed')
        browser_error.retrieval_diagnostics = RetrievalDiagnostics(
            target_host='mixed.example.com',
            strategy='browser',
            phase='browser_start',
            failure_code='browser_failure',
            http_attempted=False,
            http_retry_attempted=False,
            browser_attempted=True,
            fallback_attempted=False,
        )
        with (
            patch('smartfetch.core.http_fetch', return_value=page),
            patch('smartfetch.core.extract_content', return_value={
                'low_quality': True,
                'content': 'thin',
            }),
            patch('smartfetch.core.browser_fetch', side_effect=browser_error),
            self.assertRaises(RetrievalFailure) as raised,
        ):
            core.smart_fetch(PRIVATE_URL)

        diagnostic = raised.exception.retrieval_diagnostics
        self.assertTrue(diagnostic.http_attempted)
        self.assertTrue(diagnostic.http_retry_attempted)
        self.assertTrue(diagnostic.fallback_attempted)

    def test_unknown_browser_failure_is_safe_and_force_browser_is_not_fallback(self):
        with (
            patch('smartfetch.core.http_fetch') as http,
            patch(
                'smartfetch.core.browser_fetch',
                side_effect=Exception('unknown ' + CANARY_QUERY),
            ),
            self.assertRaises(RetrievalFailure) as raised,
        ):
            core.smart_fetch(PRIVATE_URL, force_browser=True)

        http.assert_not_called()
        diagnostic = raised.exception.retrieval_diagnostics
        self.assertEqual(diagnostic.failure_code, 'unknown')
        self.assertEqual(diagnostic.strategy, 'browser')
        self.assertFalse(diagnostic.http_attempted)
        self.assertTrue(diagnostic.browser_attempted)
        self.assertFalse(diagnostic.fallback_attempted)


class ActivityDiagnosticSafetyTests(unittest.TestCase):
    @staticmethod
    def _owned_handler():
        return next(
            handler for handler in activity._LOGGER.handlers
            if getattr(handler, '_smartfetch_activity_handler', False)
        )

    def test_diagnostic_fields_are_allowlisted_only_on_tool_failed(self):
        output = io.StringIO()
        handler = self._owned_handler()
        previous_stream = handler.setStream(output)
        fields = {
            'target_host': 'mixed.example.com',
            'strategy': 'browser',
            'phase': 'browser_navigate',
            'failure_code': 'browser_failure',
            'http_attempted': True,
            'http_retry_attempted': True,
            'browser_attempted': True,
            'fallback_attempted': True,
            'upstream_status': 503,
        }
        try:
            activity.emit_activity('tool_started', **fields)
            activity.emit_activity('tool_failed', outcome='failed', **fields)
        finally:
            handler.setStream(previous_stream)

        started, failed = [json.loads(line) for line in output.getvalue().splitlines()]
        for name in fields:
            self.assertNotIn(name, started)
            self.assertEqual(failed[name], fields[name])

    def test_invalid_diagnostic_values_are_suppressed_or_safely_defaulted(self):
        output = io.StringIO()
        handler = self._owned_handler()
        previous_stream = handler.setStream(output)
        try:
            activity.emit_activity(
                'tool_failed',
                outcome='failed',
                target_host='user:pass@example.com/' + CANARY_PATH,
                strategy=['shell'],
                phase={'eval': True},
                failure_code=['raw-' + CANARY_QUERY],
                http_attempted='yes',
                upstream_status=999,
            )
        finally:
            handler.setStream(previous_stream)

        event = json.loads(output.getvalue())
        for forbidden_field in (
            'target_host', 'strategy', 'phase', 'http_attempted',
            'upstream_status',
        ):
            self.assertNotIn(forbidden_field, event)
        self.assertEqual(event['failure_code'], 'unknown')
        self.assertNotIn(CANARY_PATH, output.getvalue())
        self.assertNotIn(CANARY_QUERY, output.getvalue())

    def test_http_502_logs_one_safe_final_diagnostic_event(self):
        diagnostic = RetrievalDiagnostics(
            target_host='mixed.example.com',
            strategy='browser',
            phase='browser_extract',
            failure_code='invalid_content',
            http_attempted=True,
            http_retry_attempted=False,
            browser_attempted=True,
            fallback_attempted=True,
        )
        failure = RetrievalFailure(
            'SmartFetch failed with ' + CANARY_PASSWORD + CANARY_PATH + CANARY_QUERY,
            diagnostic,
        )
        app = server.create_app(FREE_SETTINGS)
        with (
            patch('smartfetch.server._rate_allowed', return_value=True),
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                side_effect=failure,
            ),
            TestClient(app) as client,
            self.assertLogs('smartfetch.activity', level='INFO') as captured,
        ):
            result = client.post('/fetch', json={'url': PRIVATE_URL})

        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()['error_code'], 'fetch_failed')
        events = [json.loads(record.getMessage()) for record in captured.records]
        failed = [event for event in events if event['event'] == 'tool_failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['target_host'], 'mixed.example.com')
        self.assertEqual(failed[0]['failure_code'], 'invalid_content')
        for event in events[:-1]:
            self.assertNotIn('target_host', event)
            self.assertNotIn('failure_code', event)
        serialized = json.dumps(events)
        for forbidden in (
            CANARY_USER,
            CANARY_PASSWORD,
            CANARY_PATH,
            CANARY_QUERY,
            'api_key',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_real_retrieval_failure_flow_never_logs_boundary_canaries(self):
        app = server.create_app(FREE_SETTINGS)
        with (
            patch('smartfetch.server._rate_allowed', return_value=True),
            patch(
                'smartfetch.http_fetch.validate_public_url',
                side_effect=lambda url: url,
            ),
            patch(
                'smartfetch.http_fetch.SESSION.get',
                side_effect=[
                    requests.Timeout('http ' + CANARY_QUERY),
                    requests.Timeout('http ' + CANARY_PASSWORD),
                ],
            ),
            patch(
                'smartfetch.browser_fetch.validate_public_url',
                side_effect=lambda url: url,
            ),
            patch(
                'smartfetch.browser_fetch._chromium_path',
                side_effect=RuntimeError('browser ' + CANARY_PATH),
            ),
            TestClient(app) as client,
            self.assertLogs('smartfetch.activity', level='INFO') as captured,
        ):
            result = client.post('/fetch', json={'url': PRIVATE_URL})

        self.assertEqual(result.status_code, 502)
        events = [json.loads(record.getMessage()) for record in captured.records]
        failed = events[-1]
        self.assertEqual(failed['event'], 'tool_failed')
        self.assertEqual(failed['target_host'], 'mixed.example.com')
        self.assertEqual(failed['strategy'], 'browser')
        self.assertEqual(failed['phase'], 'browser_start')
        self.assertEqual(failed['failure_code'], 'browser_failure')
        self.assertTrue(failed['http_attempted'])
        self.assertTrue(failed['http_retry_attempted'])
        self.assertFalse(failed['browser_attempted'])
        self.assertTrue(failed['fallback_attempted'])
        serialized = json.dumps(events)
        for forbidden in (
            CANARY_USER,
            CANARY_PASSWORD,
            CANARY_PATH,
            CANARY_QUERY,
            'api_key',
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == '__main__':
    unittest.main()
