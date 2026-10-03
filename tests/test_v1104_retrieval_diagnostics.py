import asyncio
import io
import json
import logging
import socket
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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
from smartfetch.http_fetch import http_fetch
from smartfetch.payments import BASE_SEPOLIA, X402Settings


FREE_SETTINGS = X402Settings(False, None, '$0.005', BASE_SEPOLIA)
CANARY_USER = 'CANARY_USER_V1104'
CANARY_PASSWORD = 'CANARY_PASSWORD_V1104'
CANARY_PATH = 'CANARY_PATH_V1104'
CANARY_QUERY = 'CANARY_QUERY_V1104'
STREAM_FAILURE_CANARY = 'ORIGINAL_STREAM_FAILURE_V1104'
ACCESSOR_FAILURE_CANARY = 'ACCESSOR_FAILURE_V1104'
PRIVATE_URL = (
    f'https://{CANARY_USER}:{CANARY_PASSWORD}@Mixed.Example.COM:8443/'
    f'{CANARY_PATH}?api_key={CANARY_QUERY}#fragment'
)


class HostileStreamError(RuntimeError):
    @property
    def retrieval_diagnostics(self):
        raise RuntimeError(ACCESSOR_FAILURE_CANARY)


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

    def test_legacy_ipv4_literals_are_categorical_without_dns(self):
        with patch('socket.getaddrinfo') as resolve:
            for value in ('127.1', '2130706433', '0x7f000001'):
                with self.subTest(value=value):
                    self.assertEqual(
                        normalize_target_host(f'https://{value}/'),
                        'ip-literal',
                    )
        resolve.assert_not_called()

    def test_overlong_hostname_is_unknown_not_a_truncated_prefix(self):
        overlong = '.'.join(('a' * 63,) * 4)
        self.assertEqual(len(overlong), 255)
        self.assertEqual(
            normalize_target_host(f'https://{overlong}/'),
            'unknown',
        )

    def test_malformed_targets_are_safe(self):
        self.assertEqual(
            normalize_target_host(f'not-a-url/{CANARY_QUERY}'),
            'unknown',
        )


class HTTPBoundaryDiagnosticTests(unittest.TestCase):
    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_success_preserves_exact_plain_dict_contract(self, get, _validate):
        get.return_value = response(200)

        result = http_fetch(PRIVATE_URL)

        self.assertIs(type(result), dict)

    def test_ssrf_validation_dns_failure_is_dns_not_policy_rejection(self):
        with patch(
            'smartfetch.security.socket.getaddrinfo',
            side_effect=socket.gaierror(-2, 'resolver detail'),
        ):
            with self.assertRaises(ValueError) as raised:
                http_fetch('https://does-not-resolve.example/')

        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertEqual(diagnostic.strategy, 'http')
        self.assertEqual(diagnostic.phase, 'dns')
        self.assertEqual(diagnostic.failure_code, 'dns')
        self.assertFalse(diagnostic.http_attempted)

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

    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_hostile_stream_accessor_preserves_original_failure(
        self,
        get,
        _validate,
    ):
        streamed = response(200)
        streamed.iter_content = Mock(
            side_effect=HostileStreamError(STREAM_FAILURE_CANARY),
        )
        get.return_value = streamed

        with self.assertRaises(RetrievalFailure) as raised:
            http_fetch('https://stream.example/')

        self.assertEqual(str(raised.exception), STREAM_FAILURE_CANARY)
        self.assertNotIn(ACCESSOR_FAILURE_CANARY, str(raised.exception))
        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertEqual(diagnostic.strategy, 'http')
        self.assertEqual(diagnostic.phase, 'response')
        self.assertEqual(diagnostic.failure_code, 'unknown')
        self.assertTrue(diagnostic.http_attempted)


class BrowserBoundaryDiagnosticTests(unittest.TestCase):
    def test_ssrf_validation_dns_failure_is_dns_not_policy_rejection(self):
        with patch(
            'smartfetch.security.socket.getaddrinfo',
            side_effect=socket.gaierror(-2, 'resolver detail'),
        ):
            with self.assertRaises(ValueError) as raised:
                browser_module.browser_fetch(
                    'https://does-not-resolve.example/'
                )

        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertEqual(diagnostic.strategy, 'browser')
        self.assertEqual(diagnostic.phase, 'dns')
        self.assertEqual(diagnostic.failure_code, 'dns')
        self.assertFalse(diagnostic.browser_attempted)

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

    @patch('smartfetch.browser_fetch._free_port')
    @patch('smartfetch.browser_fetch.shutil.which', return_value=None)
    @patch('smartfetch.browser_fetch._chromium_path', return_value='chromium')
    @patch(
        'smartfetch.browser_fetch.validate_public_url',
        side_effect=lambda url: url,
    )
    def test_prelaunch_setup_failure_does_not_claim_browser_started(
        self,
        _validate,
        _chromium,
        _which,
        free_port,
    ):
        free_port.side_effect = RuntimeError('setup ' + CANARY_QUERY)

        with self.assertRaises(RuntimeError) as raised:
            browser_module.browser_fetch(PRIVATE_URL)

        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertEqual(diagnostic.phase, 'browser_start')
        self.assertEqual(diagnostic.failure_code, 'browser_failure')
        self.assertFalse(diagnostic.browser_attempted)

    def test_process_launch_failure_does_not_add_cleanup_side_effect(self):
        with (
            patch(
                'smartfetch.browser_fetch._chromium_path',
                return_value='chromium',
            ),
            patch('smartfetch.browser_fetch.shutil.which', return_value=None),
            patch('smartfetch.browser_fetch._free_port', return_value=9222),
            patch(
                'smartfetch.browser_fetch.tempfile.mkdtemp',
                return_value='profile',
            ),
            patch(
                'smartfetch.browser_fetch.subprocess.Popen',
                side_effect=OSError('launch failed'),
            ),
            patch('smartfetch.browser_fetch._cleanup_profile') as cleanup,
            self.assertRaises(OSError) as raised,
        ):
            browser_module._browser_fetch_locked(PRIVATE_URL)

        cleanup.assert_not_called()
        diagnostic = diagnostics_for_exception(raised.exception)
        self.assertTrue(diagnostic.browser_attempted)

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
        page = {
            'html': '<html>thin</html>',
            'final_url': 'https://mixed.example.com/',
            'status_code': 200,
        }

        def http_success(_url, *, _attempt_state=None):
            self.assertIsNotNone(_attempt_state)
            _attempt_state['retry_attempted'] = True
            return page

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
            patch('smartfetch.core.http_fetch', side_effect=http_success),
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

    def test_concurrent_failures_keep_diagnostics_request_local(self):
        http_barrier = threading.Barrier(2)
        browser_barrier = threading.Barrier(2)

        def failure(url, strategy, phase, barrier):
            barrier.wait(timeout=2)
            host = url.split('://', 1)[1].rstrip('/')
            error = RuntimeError(strategy + ' failed')
            error.retrieval_diagnostics = RetrievalDiagnostics(
                target_host=host,
                strategy=strategy,
                phase=phase,
                failure_code='unknown',
                http_attempted=strategy == 'http',
                http_retry_attempted=False,
                browser_attempted=strategy == 'browser',
                fallback_attempted=False,
            )
            raise error

        def retrieve(url):
            try:
                core.smart_fetch(url)
            except RetrievalFailure as error:
                return error.retrieval_diagnostics
            self.fail('expected retrieval failure')

        urls = ('https://alpha.example/', 'https://beta.example/')
        with (
            patch(
                'smartfetch.core.http_fetch',
                side_effect=lambda url, **_kwargs: failure(
                    url,
                    'http',
                    'connect',
                    http_barrier,
                ),
            ),
            patch(
                'smartfetch.core.browser_fetch',
                side_effect=lambda url: failure(
                    url,
                    'browser',
                    'browser_start',
                    browser_barrier,
                ),
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(executor.map(retrieve, urls))

        self.assertEqual(
            [result.target_host for result in results],
            ['alpha.example', 'beta.example'],
        )
        self.assertTrue(all(result.http_attempted for result in results))
        self.assertTrue(all(result.browser_attempted for result in results))


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

    @staticmethod
    def _emergency_attempt(url=PRIVATE_URL, *, force_browser=False, headers=None):
        output = io.StringIO()
        app = server.create_app(FREE_SETTINGS)
        with (
            patch.object(activity._HANDLER, 'stream', output),
            patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS entered')) as dns,
            TestClient(app) as client,
        ):
            result = client.post('/fetch', json={'url': url, 'force_browser': force_browser},
                                 headers=headers or {})
        dns.assert_not_called()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        return result, events, output.getvalue()

    def test_http_502_logs_one_safe_final_diagnostic_event(self):
        result, events, output = self._emergency_attempt()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()['error_code'], 'retrieval_unavailable')
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        self.assertEqual(events[0]['request_id'], result.headers['x-request-id'])
        self.assertNotIn('target_host', events[0])
        self.assertNotIn('failure_code', events[0])
        for forbidden in (CANARY_USER, CANARY_PASSWORD, CANARY_PATH, CANARY_QUERY, 'api_key'):
            self.assertNotIn(forbidden, output + result.text)

    def test_hostile_diagnostic_accessor_cannot_replace_http_502(self):
        class HostileRetrievalError(RuntimeError):
            def __getattribute__(self, name):
                if name == 'retrieval_diagnostics':
                    raise RuntimeError('accessor ' + CANARY_QUERY)
                return super().__getattribute__(name)

        with patch.object(core, 'smart_fetch', side_effect=HostileRetrievalError('raw failure')) as fetch:
            result, events, output = self._emergency_attempt()
        fetch.assert_not_called()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()['error_code'], 'retrieval_unavailable')
        self.assertEqual(len(events), 1)
        self.assertNotIn('raw failure', output + result.text)
        self.assertNotIn(CANARY_QUERY, output + result.text)

    def test_timeout_and_target_rejection_events_are_conservatively_enriched(self):
        for url in (PRIVATE_URL, 'http://127.0.0.1/private'):
            with self.subTest(url=url):
                result, events, output = self._emergency_attempt(url)
                self.assertEqual(result.status_code, 503)
                self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
                self.assertNotIn('target_host', events[0])
                self.assertNotIn('phase', events[0])
                self.assertNotIn('http_attempted', events[0])
                self.assertNotIn(url, output + result.text)

    def test_real_retrieval_failure_flow_never_logs_boundary_canaries(self):
        with patch.object(browser_module, 'browser_fetch', side_effect=AssertionError('browser entered')) as browser:
            result, events, output = self._emergency_attempt(force_browser=True)
        browser.assert_not_called()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()['error_code'], 'browser_unavailable')
        self.assertEqual([event['event'] for event in events], ['unavailable_attempt'])
        for forbidden in (CANARY_USER, CANARY_PASSWORD, CANARY_PATH, CANARY_QUERY, 'api_key'):
            self.assertNotIn(forbidden, output + result.text)

    def test_real_stream_accessor_failure_keeps_502_and_logs_no_canaries(self):
        with patch('smartfetch.http_fetch.http_fetch',
                   side_effect=AssertionError(STREAM_FAILURE_CANARY)) as fetch:
            result, events, output = self._emergency_attempt(
                'https://stream.example/', headers={'PAYMENT-SIGNATURE': ACCESSOR_FAILURE_CANARY})
        fetch.assert_not_called()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()['error_code'], 'retrieval_unavailable')
        self.assertEqual(len(events), 1)
        self.assertNotIn(STREAM_FAILURE_CANARY, output + result.text)
        self.assertNotIn(ACCESSOR_FAILURE_CANARY, output + result.text)

if __name__ == '__main__':
    unittest.main()
