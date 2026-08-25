import unittest
from unittest.mock import Mock, patch

import requests

from smartfetch.http_fetch import http_fetch


def response(status_code=200, body=b'<html>ok</html>'):
    result = requests.Response()
    result.status_code = status_code
    result.headers = {'Content-Type': 'text/html; charset=utf-8'}
    result.encoding = 'utf-8'
    result._content = body
    result._content_consumed = True
    return result


class TransientRetryTests(unittest.TestCase):
    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_retries_one_transient_status_then_succeeds(self, get, _validate):
        for status_code in (502, 503, 504):
            with self.subTest(status_code=status_code):
                transient = response(status_code)
                transient.close = Mock()
                get.side_effect = [transient, response(200)]

                result = http_fetch('https://example.com/')

                self.assertEqual(result['status_code'], 200)
                self.assertEqual(get.call_count, 2)
                transient.close.assert_called_once_with()
                get.reset_mock()

    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_retries_one_network_failure_then_succeeds(self, get, _validate):
        for failure in (
            requests.ConnectionError('reset'),
            requests.Timeout('timed out'),
        ):
            with self.subTest(failure=type(failure).__name__):
                get.side_effect = [failure, response(200)]

                result = http_fetch('https://example.com/')

                self.assertEqual(result['status_code'], 200)
                self.assertEqual(get.call_count, 2)
                get.reset_mock()

    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_reports_failure_after_exactly_one_retry(self, get, _validate):
        get.side_effect = [response(502), response(504)]

        with self.assertRaisesRegex(RuntimeError, 'HTTP status 504'):
            http_fetch('https://example.com/')

        self.assertEqual(get.call_count, 2)

    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_second_network_failure_propagates_after_one_retry(
        self, get, _validate
    ):
        for failure_type in (requests.ConnectionError, requests.Timeout):
            with self.subTest(failure=failure_type.__name__):
                get.side_effect = [failure_type('first'), failure_type('second')]

                with self.assertRaises(failure_type):
                    http_fetch('https://example.com/')

                self.assertEqual(get.call_count, 2)
                get.reset_mock()

    @patch('smartfetch.http_fetch.validate_public_url', side_effect=lambda url: url)
    @patch('smartfetch.http_fetch.SESSION.get')
    def test_does_not_retry_nontransient_http_status(self, get, _validate):
        for status_code in (404, 500):
            with self.subTest(status_code=status_code):
                get.return_value = response(status_code)

                with self.assertRaisesRegex(
                    RuntimeError, f'HTTP status {status_code}'
                ):
                    http_fetch('https://example.com/')

                self.assertEqual(get.call_count, 1)
                get.reset_mock()


if __name__ == '__main__':
    unittest.main()
