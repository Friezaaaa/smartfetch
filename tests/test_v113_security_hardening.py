"""Tests for v1.13 Phase 1: SSRF and Fetch Security Hardening."""

import io
import socket
import unittest
from unittest.mock import MagicMock, patch

import requests

from smartfetch.diagnostics import diagnostics_for_exception
from smartfetch.http_fetch import http_fetch
from smartfetch.security import (
    DNSResolutionError,
    PinnedTarget,
    is_ip_allowed,
    resolve_and_pin_public_url,
    validate_public_url,
)


def mock_response(status_code=200, body=b"<html>OK</html>", headers=None):
    resp = requests.Response()
    resp.status_code = status_code
    resp.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
    resp.encoding = "utf-8"
    resp._content = body
    resp._content_consumed = True
    return resp


class TestSSRFIPFiltering(unittest.TestCase):
    """Verifies that private, loopback, link-local, cloud-metadata, and IPv4-mapped targets are blocked."""

    def test_blocks_private_ipv4_literals(self):
        targets = [
            "http://10.0.0.1/",
            "http://10.254.0.1:80/",
            "https://172.16.0.1/",
            "https://172.31.255.255/",
            "http://192.168.0.1/",
            "http://192.168.1.100/",
            "http://127.0.0.1/",
            "http://127.0.0.2:80/",
            "http://0.0.0.0/",
            "http://255.255.255.255/",
        ]
        for url in targets:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_public_url(url)

    def test_blocks_cloud_metadata_targets(self):
        targets = [
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.169.254/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://metadata/computeMetadata/v1/",
            "http://instance-data/latest/meta-data/",
        ]
        for url in targets:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_public_url(url)

    def test_blocks_ipv6_special_ranges(self):
        targets = [
            "http://[::1]/",
            "http://[::]/",
            "http://[fc00::1]/",
            "http://[fd12:3456:789a::1]/",
            "http://[fe80::1]/",
        ]
        for url in targets:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_public_url(url)

    def test_blocks_ipv4_mapped_ipv6(self):
        targets = [
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:169.254.169.254]/",
            "http://[::ffff:10.0.0.1]/",
            "http://[::ffff:8.8.8.8]/",  # IPv4-mapped rejected outright
        ]
        for url in targets:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_public_url(url)

    def test_blocks_internal_domain_suffixes(self):
        suffixes = [
            "http://service.internal/",
            "http://myhost.local/",
            "http://app.localhost/",
            "http://router.home/",
            "http://printer.lan/",
            "http://corp-portal.corp/",
            "http://database.private/",
        ]
        for url in suffixes:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_public_url(url)

    def test_blocks_non_standard_ports(self):
        invalid_ports = [
            "http://example.com:8080/",
            "https://example.com:8443/",
            "http://example.com:22/",
            "http://example.com:6379/",
            "http://example.com:3000/",
            "http://example.com:0/",
        ]
        for url in invalid_ports:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_public_url(url)

    def test_is_ip_allowed_helper(self):
        self.assertFalse(is_ip_allowed("127.0.0.1"))
        self.assertFalse(is_ip_allowed("10.0.0.1"))
        self.assertFalse(is_ip_allowed("169.254.169.254"))
        self.assertFalse(is_ip_allowed("::1"))
        self.assertFalse(is_ip_allowed("::ffff:127.0.0.1"))
        self.assertFalse(is_ip_allowed("::ffff:8.8.8.8"))
        self.assertFalse(is_ip_allowed("not-an-ip"))
        self.assertTrue(is_ip_allowed("8.8.8.8"))
        self.assertTrue(is_ip_allowed("1.1.1.1"))
        self.assertTrue(is_ip_allowed("2606:4700:4700::1111"))


class TestDNSResolutionAndPinning(unittest.TestCase):
    """Verifies DNS pre-resolution, validation of all resolved records, and PinnedTarget creation."""

    @patch("smartfetch.security.socket.getaddrinfo")
    def test_resolves_and_pins_public_target(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 443)),
        ]
        target = resolve_and_pin_public_url("https://example.com/path")
        self.assertIsInstance(target, PinnedTarget)
        self.assertEqual(target.hostname, "example.com")
        self.assertEqual(target.port, 443)
        self.assertEqual(target.scheme, "https")
        self.assertEqual(target.addresses, ("93.184.216.34", "93.184.216.35"))

    @patch("smartfetch.security.socket.getaddrinfo")
    def test_blocks_if_any_resolved_ip_is_private(self, mock_getaddrinfo):
        # Round-robin / dual-homed with one private IP
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]
        with self.assertRaises(ValueError) as ctx:
            resolve_and_pin_public_url("https://example.com/")
        self.assertIn("Private/local/network-metadata", str(ctx.exception))

    @patch("smartfetch.security.socket.getaddrinfo")
    def test_dns_gaierror_raises_dns_resolution_error(self, mock_getaddrinfo):
        mock_getaddrinfo.side_effect = socket.gaierror(-2, "Name or service not known")
        with self.assertRaises(DNSResolutionError):
            resolve_and_pin_public_url("https://nonexistent-domain-404.example/")


class TestRedirectValidationAndHardening(unittest.TestCase):
    """Verifies redirect destination evaluation, redirect loops, and redirect depth limits."""

    @patch("smartfetch.http_fetch.validate_public_url")
    @patch("smartfetch.http_fetch.SESSION.get")
    def test_redirect_to_blocked_target_aborts_with_policy_rejection(self, mock_get, mock_validate):
        # First request returns 302 redirecting to metadata service
        redirect_resp = mock_response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )
        mock_get.return_value = redirect_resp

        def fake_validate(target_url):
            if "169.254" in target_url:
                raise ValueError("Private/local/network-metadata targets are blocked")
            return target_url

        mock_validate.side_effect = fake_validate

        with self.assertRaises(ValueError) as raised:
            http_fetch("https://public-site.example/")

        diag = diagnostics_for_exception(raised.exception)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.phase, "redirect")
        self.assertEqual(diag.failure_code, "policy_rejection")

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.SESSION.get")
    def test_redirect_loop_aborts_with_policy_rejection(self, mock_get, _validate):
        resp_a = mock_response(302, headers={"Location": "https://example.com/b"})
        resp_b = mock_response(302, headers={"Location": "https://example.com/a"})
        mock_get.side_effect = [resp_a, resp_b, resp_a]

        with self.assertRaises(RuntimeError) as raised:
            http_fetch("https://example.com/a")

        self.assertIn("Redirect loop detected", str(raised.exception))
        diag = diagnostics_for_exception(raised.exception)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.phase, "redirect")
        self.assertEqual(diag.failure_code, "policy_rejection")

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.SESSION.get")
    def test_too_many_redirects_aborts_with_policy_rejection(self, mock_get, _validate):
        # 6 redirects exceeds MAX_REDIRECTS (5)
        redirects = [
            mock_response(302, headers={"Location": f"https://example.com/step{i}"})
            for i in range(1, 7)
        ]
        mock_get.side_effect = redirects

        with self.assertRaises(RuntimeError) as raised:
            http_fetch("https://example.com/step0")

        self.assertIn("Too many redirects", str(raised.exception))
        diag = diagnostics_for_exception(raised.exception)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.phase, "redirect")
        self.assertEqual(diag.failure_code, "policy_rejection")

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.SESSION.get")
    def test_redirect_response_is_closed(self, mock_get, _validate):
        resp = mock_response(302, headers={"Location": "https://example.com/dest"})
        resp.close = MagicMock()
        final_resp = mock_response(200, body=b"<html>OK</html>")
        final_resp.close = MagicMock()
        mock_get.side_effect = [resp, final_resp]

        http_fetch("https://example.com/start")
        resp.close.assert_called_once()
        final_resp.close.assert_called_once()


class TestSocketLevelDNSRebindingImmunity(unittest.TestCase):
    """Verifies that SafeHTTPConnection validates destination IP at socket connect time."""

    @patch("urllib3.connection.HTTPConnection._new_conn")
    def test_socket_level_verification_blocks_private_ip(self, mock_super_conn):
        from smartfetch.security import SafeHTTPConnection
        mock_sock = MagicMock()
        mock_sock.getpeername.return_value = ("169.254.169.254", 80)
        mock_super_conn.return_value = mock_sock

        conn = SafeHTTPConnection("rebind.example.com", 80)
        with self.assertRaises(ValueError) as ctx:
            conn._new_conn()

        self.assertIn("Private/local/network-metadata", str(ctx.exception))
        mock_sock.close.assert_called_once()

    @patch("urllib3.connection.HTTPConnection._new_conn")
    def test_socket_level_verification_allows_public_ip(self, mock_super_conn):
        from smartfetch.security import SafeHTTPConnection
        mock_sock = MagicMock()
        mock_sock.getpeername.return_value = ("93.184.216.34", 80)
        mock_super_conn.return_value = mock_sock

        conn = SafeHTTPConnection("example.com", 80)
        sock = conn._new_conn()
        self.assertIs(sock, mock_sock)
        mock_sock.close.assert_not_called()


class TestResponseSizeCeilings(unittest.TestCase):
    """Verifies that Content-Length header and streaming chunks are bounded by MAX_RESPONSE_BYTES."""

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.SESSION.get")
    def test_declared_content_length_exceeding_ceiling_rejected(self, mock_get, _validate):
        # Default ceiling is 10MB (10485760 bytes)
        resp = mock_response(
            200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": "20000000",
            },
        )
        mock_get.return_value = resp

        with self.assertRaises(RuntimeError) as raised:
            http_fetch("https://example.com/huge.html")

        self.assertIn("Response too large (20000000 bytes)", str(raised.exception))
        diag = diagnostics_for_exception(raised.exception)
        self.assertEqual(diag.phase, "response")
        self.assertEqual(diag.failure_code, "invalid_content")

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.SESSION.get")
    def test_streaming_chunks_exceeding_ceiling_rejected(self, mock_get, _validate):
        resp = mock_response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
        # Simulate an infinite / oversized chunk stream without Content-Length header
        resp.iter_content = MagicMock(return_value=iter([b"A" * 1048576] * 12))  # 12 MB
        mock_get.return_value = resp

        with self.assertRaises(RuntimeError) as raised:
            http_fetch("https://example.com/stream")

        self.assertIn("byte limit", str(raised.exception))
        diag = diagnostics_for_exception(raised.exception)
        self.assertEqual(diag.phase, "response")
        self.assertEqual(diag.failure_code, "invalid_content")


class TestTimeoutPolicies(unittest.TestCase):
    """Verifies granular timeout configuration."""

    def test_timeout_defaults_are_granular(self):
        from smartfetch.http_fetch import (
            CONNECT_TIMEOUT,
            FETCH_TIMEOUT,
            READ_TIMEOUT,
            TIMEOUT,
        )
        self.assertEqual(CONNECT_TIMEOUT, 3.0)
        self.assertEqual(READ_TIMEOUT, 5.0)
        self.assertEqual(FETCH_TIMEOUT, 10.0)
        self.assertEqual(TIMEOUT, (3.0, 5.0))


if __name__ == "__main__":
    unittest.main()
