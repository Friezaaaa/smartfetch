"""Synthetic peers exercise the real local proxy, never an external host."""

import http.client
from contextlib import contextmanager, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from smartfetch.browser_prototype.broker import Broker, _ProxyHandler, _ProxyServer
from smartfetch.browser_prototype.policy import DestinationPolicy


class FixturePolicy(DestinationPolicy):
    def port_allowed(self, scheme, port):
        return scheme in {"http", "https"} and port == self.fixture_port

    def address_allowed(self, ip):
        return ip == "127.0.0.1"


class FixtureHandler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Host"),
                          self.headers.get("Proxy-Authorization")))
        if self.path == "/stall":
            time.sleep(0.5)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://other.test:{self.server.server_port}/done")
            self.end_headers()
            return
        body = b"x" * 2048 if self.path == "/large" else b"synthetic-page"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            # A bounded proxy timeout deliberately closes the fixture peer.
            pass

    def log_message(self, *_args):
        pass


class TLSFixtureHandler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Host")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"https://other.test:{self.server.server_port}/done")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"synthetic-tls-page"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@contextmanager
def synthetic_tls_fixture():
    openssl = shutil.which("openssl")
    if openssl is None:
        raise unittest.SkipTest("local OpenSSL unavailable for synthetic TLS fixture")
    with tempfile.TemporaryDirectory(prefix="smartfetch-synthetic-tls-") as directory:
        cert = Path(directory) / "fixture.pem"
        key = Path(directory) / "fixture.key"
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-sha256", "-days", "1", "-keyout", str(key), "-out", str(cert),
             "-subj", "/CN=fixture.test",
             "-addext", "subjectAltName=DNS:fixture.test,DNS:other.test"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), TLSFixtureHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, cert
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if thread.is_alive():
                raise AssertionError("synthetic TLS fixture did not stop")


class BrokerTests(unittest.TestCase):
    def setUp(self):
        FixtureHandler.seen = []
        self.fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.fixture_thread = threading.Thread(target=self.fixture.serve_forever,
                                               daemon=True)
        self.fixture_thread.start()
        self.policy = FixturePolicy()
        self.policy.fixture_port = self.fixture.server_port
        self.broker = Broker(
            self.policy,
            resolver=lambda host, port: ["127.0.0.1"],
            connector=lambda ip, port, timeout: socket.create_connection(
                (ip, port), timeout=timeout
            ),
            total_deadline=time.monotonic() + 5,
            max_request_bytes=4096,
            max_tunnel_bytes_each_direction=1024,
            max_connections=4,
        )
        self.proxy_host, self.proxy_port = self.broker.start()

    def tearDown(self):
        self.assertTrue(self.broker.close())
        self.fixture.shutdown()
        self.fixture.server_close()
        self.fixture_thread.join(timeout=2)
        self.assertFalse(self.fixture_thread.is_alive())

    def _http(self, path, headers=None):
        connection = http.client.HTTPConnection(self.proxy_host, self.proxy_port,
                                                timeout=2)
        connection.request("GET", f"http://fixture.test:{self.fixture.server_port}{path}",
                           headers=headers or {})
        response = connection.getresponse()
        result = (response.status, response.read(), response.getheader("Location"))
        connection.close()
        return result

    def _tls_get(self, host, port, path, cert):
        raw = socket.create_connection((self.proxy_host, self.proxy_port), timeout=2)
        try:
            raw.sendall(
                f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
            )
            reply = bytearray()
            while b"\r\n\r\n" not in reply and len(reply) < 512:
                reply.extend(raw.recv(1))
            self.assertIn(b"200 Connection Established", reply)
            context = ssl.create_default_context(cafile=str(cert))
            self.assertTrue(context.check_hostname)
            with context.wrap_socket(raw, server_hostname=host) as tunnel:
                tunnel.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                    "Connection: close\r\n\r\n".encode()
                )
                response = http.client.HTTPResponse(tunnel)
                response.begin()
                return response.status, response.read(), response.getheader("Location")
        finally:
            raw.close()

    def test_http_proxy_rewrites_host_and_drops_proxy_credentials(self):
        status, body, _ = self._http("/story", {"Proxy-Authorization": "secret"})
        self.assertEqual((status, body), (200, b"synthetic-page"))
        self.assertEqual(FixtureHandler.seen,
                         [("/story", f"fixture.test:{self.fixture.server_port}", None)])
        self.assertEqual(self.broker.allowed, 1)

    def test_http_redirect_requires_second_destination_decision(self):
        first_status, _, location = self._http("/redirect")
        self.assertEqual(first_status, 302)
        self.assertEqual(location,
                         f"http://other.test:{self.fixture.server_port}/done")
        connection = http.client.HTTPConnection(self.proxy_host, self.proxy_port,
                                                timeout=2)
        connection.request("GET", location)
        second = connection.getresponse()
        self.assertEqual((second.status, second.read()), (200, b"synthetic-page"))
        connection.close()
        self.assertEqual(self.broker.allowed, 2)
        self.assertEqual(FixtureHandler.seen[-1][1],
                         f"other.test:{self.fixture.server_port}")

    def test_validated_connect_relays_opaque_bytes(self):
        with socket.create_connection((self.proxy_host, self.proxy_port), timeout=2) as client:
            client.sendall(
                f"CONNECT fixture.test:{self.fixture.server_port} HTTP/1.1\r\n"
                f"Host: fixture.test:{self.fixture.server_port}\r\n\r\n".encode()
            )
            self.assertIn(b"200 Connection Established", client.recv(256))
            client.sendall(b"GET /story HTTP/1.1\r\nHost: fixture.test\r\nConnection: close\r\n\r\n")
            data = bytearray()
            while b"synthetic-page" not in data:
                chunk = client.recv(1024)
                if not chunk:
                    break
                data.extend(chunk)
            self.assertIn(b"synthetic-page", data)
        self.assertEqual(self.broker.connects, 1)

    def test_https_connect_relays_real_tls_with_client_certificate_validation(self):
        with synthetic_tls_fixture() as (server, cert):
            TLSFixtureHandler.seen = []
            self.policy.fixture_port = server.server_port
            self.broker.max_tunnel_bytes_each_direction = 64_000
            status, body, _ = self._tls_get(
                "fixture.test", server.server_port, "/story", cert
            )
            self.assertEqual((status, body), (200, b"synthetic-tls-page"))
            self.assertEqual(TLSFixtureHandler.seen, [
                ("/story", f"fixture.test:{server.server_port}")
            ])
            self.assertEqual((self.broker.allowed, self.broker.connects), (1, 1))

    def test_https_redirect_uses_a_second_validated_connect(self):
        with synthetic_tls_fixture() as (server, cert):
            TLSFixtureHandler.seen = []
            self.policy.fixture_port = server.server_port
            self.broker.max_tunnel_bytes_each_direction = 64_000
            first = self._tls_get("fixture.test", server.server_port, "/redirect", cert)
            self.assertEqual(first, (
                302, b"", f"https://other.test:{server.server_port}/done"
            ))
            second = self._tls_get("other.test", server.server_port, "/done", cert)
            self.assertEqual(second[:2], (200, b"synthetic-tls-page"))
            self.assertEqual(TLSFixtureHandler.seen, [
                ("/redirect", f"fixture.test:{server.server_port}"),
                ("/done", f"other.test:{server.server_port}"),
            ])
            self.assertEqual((self.broker.allowed, self.broker.connects), (2, 2))

    def test_arbitrary_connect_is_denied_before_connector(self):
        attempted = []
        self.broker.connector = lambda *args: attempted.append(args)
        with socket.create_connection((self.proxy_host, self.proxy_port), timeout=2) as client:
            client.sendall(b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n")
            self.assertIn(b"403", client.recv(256))
        self.assertEqual(attempted, [])
        self.assertEqual(self.broker.denied, 1)

    def test_wrong_connected_peer_fails_before_forwarding(self):
        self.broker.resolver = lambda host, port: ["8.8.8.8"]
        self.broker.policy = DestinationPolicy()
        self.broker.connector = lambda ip, port, timeout: socket.create_connection(
            ("127.0.0.1", self.fixture.server_port), timeout=timeout
        )
        with socket.create_connection((self.proxy_host, self.proxy_port), timeout=2) as client:
            client.sendall(b"CONNECT fixture.test:443 HTTP/1.1\r\n\r\n")
            self.assertIn(b"403", client.recv(256))
        self.assertEqual(FixtureHandler.seen, [])

    def test_https_absolute_get_is_not_plaintext_forwarded(self):
        attempted = []
        self.broker.connector = lambda *args: attempted.append(args)
        connection = http.client.HTTPConnection(self.proxy_host, self.proxy_port,
                                                timeout=2)
        connection.request("GET", f"https://fixture.test:{self.fixture.server_port}/secret")
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        self.assertEqual(attempted, [])

    def test_failed_peer_readback_closes_socket(self):
        class BrokenPeer:
            closed = False

            def getpeername(self):
                raise OSError("synthetic readback failure")

            def close(self):
                self.closed = True

        upstream = BrokenPeer()
        self.broker.connector = lambda ip, port, timeout: upstream
        with socket.create_connection((self.proxy_host, self.proxy_port), timeout=2) as client:
            client.sendall(
                f"CONNECT fixture.test:{self.fixture.server_port} HTTP/1.1\r\n\r\n".encode()
            )
            self.assertIn(b"502", client.recv(256))
        self.assertTrue(upstream.closed)

    def test_oversized_headers_never_connect(self):
        attempted = []
        self.broker.connector = lambda *args: attempted.append(args)
        connection = http.client.HTTPConnection(self.proxy_host, self.proxy_port,
                                                timeout=2)
        connection.request("GET", f"http://fixture.test:{self.fixture.server_port}/story",
                           headers={"X-Padding": "x" * 5000})
        response = connection.getresponse()
        self.assertEqual(response.status, 431)
        response.read()
        connection.close()
        self.assertEqual(attempted, [])

    def test_oversized_response_is_not_partially_delivered(self):
        self.broker.max_tunnel_bytes_each_direction = 256
        status, body, _ = self._http("/large")
        self.assertEqual(status, 502)
        self.assertEqual(body, b"")

    def test_malformed_absolute_url_is_finite_denial(self):
        with socket.create_connection((self.proxy_host, self.proxy_port), timeout=2) as client:
            client.sendall(b"GET http://[broken/ HTTP/1.1\r\nHost: ignored\r\n\r\n")
            self.assertIn(b"403", client.recv(256))
        self.assertEqual(FixtureHandler.seen, [])

    def test_upstream_stall_returns_finite_timeout(self):
        self.broker.total_deadline = time.monotonic() + 0.15
        status, body, _ = self._http("/stall")
        self.assertEqual((status, body), (503, b""))

    def test_connect_tunnel_byte_limit_closes_before_forward(self):
        self.broker.max_tunnel_bytes_each_direction = 32
        with socket.create_connection((self.proxy_host, self.proxy_port), timeout=2) as client:
            client.sendall(
                f"CONNECT fixture.test:{self.fixture.server_port} HTTP/1.1\r\n\r\n".encode()
            )
            self.assertIn(b"200 Connection Established", client.recv(256))
            client.sendall(b"GET /story HTTP/1.1\r\nHost: fixture.test\r\n\r\n")
            try:
                self.assertEqual(client.recv(256), b"")
            except ConnectionResetError:
                pass
        self.assertEqual(FixtureHandler.seen, [])

    def test_expired_deadline_never_connects(self):
        self.broker.total_deadline = time.monotonic() - 1
        attempted = []
        self.broker.connector = lambda *args: attempted.append(args)
        status, _, _ = self._http("/story")
        self.assertEqual(status, 503)
        self.assertEqual(attempted, [])

    def test_expired_deadline_replies_before_partial_headers(self):
        self.broker.total_deadline = time.monotonic() - 1
        client, request = socket.socketpair()
        try:
            self.broker._server.process_request(request, ("127.0.0.1", 0))
            client.settimeout(1)
            self.assertIn(b"503 Service Unavailable", client.recv(256))
        finally:
            client.close()
            request.close()

    def test_close_cancels_a_slow_header_client(self):
        client = socket.create_connection((self.proxy_host, self.proxy_port), timeout=2)
        try:
            client.sendall(b"GET / HTTP/1.1\r\nX-Stall: ")
            until = time.monotonic() + 1
            while not self.broker._threads and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(self.broker._threads, "slow client was not admitted")
            started = time.monotonic()
            closed = self.broker.close()
            self.assertTrue(closed, (
                f"serve_alive={self.broker._serve_thread.is_alive()} "
                f"handlers={len(self.broker._threads)} "
                f"clients={len(self.broker._clients)} "
                f"upstreams={len(self.broker._active)} "
                f"elapsed={time.monotonic() - started:.3f}"
            ))
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            client.close()

    def test_operation_request_budget_denies_next_destination(self):
        self.broker.max_requests = 2
        self.assertEqual(self._http("/one")[0], 200)
        self.assertEqual(self._http("/two")[0], 200)
        status, body, _ = self._http("/three")
        self.assertEqual((status, body), (429, b""))
        self.assertEqual([path for path, _, _ in FixtureHandler.seen],
                         ["/one", "/two"])

    def test_operation_byte_budget_spans_separate_connections(self):
        self.broker.max_tunnel_bytes_each_direction = 4096
        self.broker.max_operation_bytes = 3000
        self.assertEqual(self._http("/large")[:2], (200, b"x" * 2048))
        status, body, _ = self._http("/large")
        self.assertEqual((status, body), (502, b""))
        self.assertLessEqual(self.broker.operation_bytes, 3000)

    def test_connect_forward_uses_remaining_deadline_not_admission_timeout(self):
        upstream, peer = socket.socketpair()
        sent = threading.Event()
        observed = []

        class RecordingSocket:
            def getpeername(self):
                return ("127.0.0.1", self_port)

            def fileno(self):
                return upstream.fileno()

            def settimeout(self, value):
                upstream.settimeout(value)

            def recv(self, size):
                return upstream.recv(size)

            def sendall(self, data):
                observed.append((upstream.gettimeout(),
                                 self_broker.total_deadline - time.monotonic()))
                sent.set()
                upstream.sendall(data)

            def close(self):
                upstream.close()

            def shutdown(self, how):
                upstream.shutdown(how)

        self_port = self.fixture.server_port
        self_broker = self.broker
        self.broker.connector = lambda *_args: RecordingSocket()
        self.broker.total_deadline = time.monotonic() + 0.4
        try:
            with socket.create_connection((self.proxy_host, self.proxy_port), timeout=2) as client:
                client.sendall(f"CONNECT fixture.test:{self_port} HTTP/1.1\r\n\r\n".encode())
                self.assertIn(b"200 Connection Established", client.recv(256))
                time.sleep(0.18)
                client.sendall(b"x")
                self.assertTrue(sent.wait(0.3))
            timeout, remaining = observed[0]
            self.assertLessEqual(timeout, remaining + 0.03)
        finally:
            peer.close()

    def test_dns_subprocess_timeout_stops_work_before_its_late_side_effect(self):
        from smartfetch.browser_prototype.resolver import run_bounded_child

        with tempfile.TemporaryDirectory(prefix="smartfetch-dns-timeout-") as root:
            marker = Path(root) / "late-marker"
            code = ("import pathlib,sys,time; time.sleep(0.8); "
                    "pathlib.Path(sys.argv[1]).write_text('escaped')")
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                run_bounded_child(
                    [sys.executable, "-I", "-c", code, str(marker)],
                    deadline=started + 0.35,
                )
            self.assertLess(time.monotonic() - started, 0.6)
            time.sleep(max(0, started + 1.0 - time.monotonic()))
            self.assertFalse(marker.exists(), "timed-out resolver child kept running")

    def test_default_dns_path_uses_bounded_child_not_parent_getaddrinfo(self):
        from smartfetch.browser_prototype import resolver as child_resolver

        original = child_resolver._RESOLVE_CODE
        child_resolver._RESOLVE_CODE = "import sys; sys.stdout.write('[\"127.0.0.1\"]')"
        self.broker.resolver = None
        try:
            self.assertEqual(self._http("/story")[:2], (200, b"synthetic-page"))
        finally:
            child_resolver._RESOLVE_CODE = original

    def test_uncertain_dns_child_cleanup_prevents_clean_broker_close(self):
        extra = Broker(
            self.policy, resolver=None,
            connector=self.broker.connector,
            total_deadline=time.monotonic() + 2,
        )
        host, port = extra.start()
        with patch("smartfetch.browser_prototype.broker._system_resolver",
                   side_effect=RuntimeError("resolver_cleanup_uncertain")):
            connection = http.client.HTTPConnection(host, port, timeout=2)
            connection.request("GET", f"http://fixture.test:{self.fixture.server_port}/story")
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (403, b""))
            connection.close()
        self.assertFalse(extra.close())
        self.assertEqual(FixtureHandler.seen, [])

    def test_closed_client_during_finite_reply_does_not_raise(self):
        class ClosedClient:
            def write(self, _data):
                raise ConnectionResetError("synthetic closed client")

        handler = object.__new__(_ProxyHandler)
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET / HTTP/1.1"
        handler.command = "GET"
        handler.wfile = ClosedClient()
        handler.close_connection = False
        handler._reply(503)
        self.assertTrue(handler.close_connection)

    def test_unexpected_handler_error_is_counted_without_raw_stderr(self):
        server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler)
        server.broker = self.broker
        output = io.StringIO()
        try:
            with redirect_stderr(output):
                try:
                    raise ConnectionResetError("synthetic-private-marker")
                except ConnectionResetError:
                    server.handle_error(None, ("127.0.0.1", 0))
        finally:
            server.server_close()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.broker.handler_failures, 1)


if __name__ == "__main__":
    unittest.main()
