"""Local-only framed IPC tests for the disabled Linux browser worker."""

import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import threading
from unittest.mock import patch

from smartfetch.browser_prototype.broker import Broker
from smartfetch.browser_prototype.policy import DestinationPolicy


class FramedRelayTests(unittest.TestCase):
    def test_closed_socket_during_send_is_a_finite_relay_error(self):
        from smartfetch.browser_prototype.relay import RelayError, send_frame

        sender, receiver = socket.socketpair()
        sender.close()
        try:
            with self.assertRaises(RelayError) as caught:
                send_frame(sender, b"synthetic", deadline=time.monotonic() + 1)
            self.assertEqual(caught.exception.code, "relay_io")
        finally:
            receiver.close()

    def test_closed_socket_during_receive_is_a_finite_relay_error(self):
        from smartfetch.browser_prototype.relay import RelayError, recv_frame

        sender, receiver = socket.socketpair()
        receiver.close()
        try:
            with self.assertRaises(RelayError) as caught:
                recv_frame(receiver, deadline=time.monotonic() + 1)
            self.assertEqual(caught.exception.code, "relay_io")
        finally:
            sender.close()

    def test_closed_plain_socket_is_a_finite_relay_error(self):
        from smartfetch.browser_prototype.relay import RelayError, _recv_plain

        sender, receiver = socket.socketpair()
        receiver.close()
        try:
            with self.assertRaises(RelayError) as caught:
                _recv_plain(receiver, deadline=time.monotonic() + 1)
            self.assertEqual(caught.exception.code, "relay_io")
        finally:
            sender.close()

    def test_repeated_nonblocking_readiness_does_not_recurse(self):
        from smartfetch.browser_prototype import relay

        class Socket:
            def __init__(self):
                self.calls = 0

            def setblocking(self, _enabled):
                pass

            def recv(self, _limit):
                self.calls += 1
                if self.calls <= 1100:
                    raise BlockingIOError()
                return b"ready"

        sock = Socket()
        with patch.object(relay, "_wait"):
            self.assertEqual(relay._recv_plain(sock, deadline=time.monotonic() + 1),
                             b"ready")
        self.assertEqual(sock.calls, 1101)

    def test_oversized_frame_is_rejected_before_body_read(self):
        from smartfetch.browser_prototype.relay import RelayError, recv_frame

        sender, receiver = socket.socketpair()
        try:
            sender.sendall(struct.pack("!I", 8193))
            with self.assertRaises(RelayError) as caught:
                recv_frame(receiver, deadline=time.monotonic() + 0.3)
            self.assertEqual(caught.exception.code, "frame_too_large")
        finally:
            sender.close()
            receiver.close()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires real AF_UNIX")
    def test_worker_local_proxy_reaches_fixture_only_through_unix_bridge(self):
        from smartfetch.browser_prototype.relay import BridgeServer, WorkerRelay

        seen = []

        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = b"broker-only"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        class FixturePolicy(DestinationPolicy):
            def port_allowed(self, scheme, port):
                return scheme == "http" and port == fixture.server_port

            def address_allowed(self, ip):
                return ip == "127.0.0.1"

        fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        fixture_thread.start()
        deadline = time.monotonic() + 4
        broker = Broker(FixturePolicy(), resolver=lambda *_: ["127.0.0.1"],
                        total_deadline=deadline)
        broker_address = broker.start()
        try:
            with tempfile.TemporaryDirectory(prefix="sfipc-") as root:
                socket_path = str(Path(root) / "relay.sock")
                bridge = BridgeServer(socket_path, broker_address,
                                      operation_id=b"f" * 16, deadline=deadline)
                bridge.start()
                worker = WorkerRelay(socket_path, operation_id=b"f" * 16,
                                     deadline=deadline)
                worker_address = worker.start()
                try:
                    client = http.client.HTTPConnection(*worker_address, timeout=2)
                    client.request("GET", f"http://fixture.test:{fixture.server_port}/page")
                    response = client.getresponse()
                    self.assertEqual((response.status, response.read()),
                                     (200, b"broker-only"))
                    client.close()
                    self.assertEqual(seen, ["/page"])
                    self.assertEqual(broker.allowed, 1)
                finally:
                    self.assertTrue(worker.close())
                    self.assertTrue(bridge.close())
                    self.assertFalse(Path(socket_path).exists())
        finally:
            self.assertTrue(broker.close())
            fixture.shutdown()
            fixture.server_close()
            fixture_thread.join(timeout=2)
            self.assertFalse(fixture_thread.is_alive())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires real AF_UNIX")
    def test_wrong_operation_id_never_reaches_broker(self):
        from smartfetch.browser_prototype.relay import BridgeServer, WorkerRelay

        class Policy(DestinationPolicy):
            def address_allowed(self, _ip):
                return True

        deadline = time.monotonic() + 3
        broker = Broker(Policy(), resolver=lambda *_: ["127.0.0.1"],
                        total_deadline=deadline)
        broker_address = broker.start()
        try:
            with tempfile.TemporaryDirectory(prefix="sfipc-") as root:
                path = str(Path(root) / "relay.sock")
                bridge = BridgeServer(path, broker_address, operation_id=b"a" * 16,
                                      deadline=deadline)
                bridge.start()
                worker = WorkerRelay(path, operation_id=b"b" * 16,
                                     deadline=deadline)
                address = worker.start()
                try:
                    client = socket.create_connection(address, timeout=1)
                    client.sendall(b"GET http://fixture.test/ HTTP/1.1\r\n\r\n")
                    self.assertEqual(client.recv(1024), b"")
                    client.close()
                    self.assertEqual(broker.allowed, 0)
                    self.assertEqual(broker.denied, 0)
                    self.assertEqual(bridge.denied_handshakes, 1)
                finally:
                    self.assertTrue(worker.close())
                    self.assertTrue(bridge.close())
        finally:
            self.assertTrue(broker.close())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires real AF_UNIX")
    def test_bridge_start_failure_removes_its_socket_inode(self):
        from smartfetch.browser_prototype.relay import BridgeServer

        with tempfile.TemporaryDirectory(prefix="sfipc-") as root:
            path = Path(root) / "relay.sock"
            bridge = BridgeServer(str(path), ("127.0.0.1", 12345),
                                  operation_id=b"f" * 16,
                                  deadline=time.monotonic() + 1)
            with patch("os.chmod", side_effect=OSError("synthetic")):
                with self.assertRaises(OSError):
                    bridge.start()
            self.assertFalse(path.exists())

    def test_bounded_frame_round_trip_and_eof(self):
        from smartfetch.browser_prototype.relay import recv_frame, send_frame

        sender, receiver = socket.socketpair()
        try:
            deadline = time.monotonic() + 1
            send_frame(sender, b"synthetic-payload", deadline=deadline)
            send_frame(sender, b"", deadline=deadline)
            self.assertEqual(recv_frame(receiver, deadline=deadline),
                             b"synthetic-payload")
            self.assertEqual(recv_frame(receiver, deadline=deadline), b"")
        finally:
            sender.close()
            receiver.close()

    def test_incomplete_frame_expires_without_raw_error(self):
        from smartfetch.browser_prototype.relay import RelayError, recv_frame

        sender, receiver = socket.socketpair()
        try:
            sender.sendall(struct.pack("!I", 5) + b"ab")
            with self.assertRaises(RelayError) as caught:
                recv_frame(receiver, deadline=time.monotonic() + 0.1)
            self.assertEqual(caught.exception.code, "relay_timeout")
        finally:
            sender.close()
            receiver.close()


class WorkerBoundaryTests(unittest.TestCase):
    def test_rejects_upstream_interface_before_browser_start(self):
        from smartfetch.browser_prototype.worker import WorkerBoundaryError, inspect_boundary

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "interfaces").mkdir()
            (root / "interfaces" / "lo").touch()
            (root / "interfaces" / "eth0").touch()
            (root / "fd").mkdir()
            with self.assertRaises(WorkerBoundaryError) as caught:
                inspect_boundary(interfaces=root / "interfaces", fds=root / "fd",
                                 routes=root / "route", environment={},
                                 expected_uid=None)
            self.assertEqual(caught.exception.code, "worker_network_boundary")

    def test_rejects_inherited_socket_fd_before_relay_start(self):
        from smartfetch.browser_prototype.worker import WorkerBoundaryError, inspect_boundary

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "interfaces").mkdir()
            (root / "interfaces" / "lo").touch()
            (root / "fd").mkdir()
            (root / "fd" / "3").touch()
            (root / "route").write_text("Iface Destination Gateway\n", encoding="ascii")
            with patch("os.readlink", return_value="socket:[12345]"):
                with self.assertRaises(WorkerBoundaryError) as caught:
                    inspect_boundary(interfaces=root / "interfaces", fds=root / "fd",
                                     routes=root / "route", environment={},
                                     expected_uid=None)
            self.assertEqual(caught.exception.code, "worker_inherited_fd")

    def test_requires_exact_non_root_uid(self):
        from smartfetch.browser_prototype.worker import WorkerBoundaryError, inspect_boundary

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "interfaces").mkdir()
            (root / "interfaces" / "lo").touch()
            (root / "fd").mkdir()
            with patch("os.geteuid", return_value=0, create=True):
                with self.assertRaises(WorkerBoundaryError) as caught:
                    inspect_boundary(interfaces=root / "interfaces", fds=root / "fd",
                                     routes=root / "route", environment={},
                                     expected_uid=10001)
            self.assertEqual(caught.exception.code, "worker_uid")

    def test_rejects_a_default_route_even_if_only_loopback_is_listed(self):
        from smartfetch.browser_prototype.worker import WorkerBoundaryError, inspect_boundary

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "interfaces").mkdir()
            (root / "interfaces" / "lo").touch()
            (root / "fd").mkdir()
            (root / "route").write_text(
                "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
                "lo 00000000 00000000 0001 0 0 0 00000000 0 0 0\n",
                encoding="ascii",
            )
            with self.assertRaises(WorkerBoundaryError) as caught:
                inspect_boundary(interfaces=root / "interfaces", fds=root / "fd",
                                 routes=root / "route", environment={},
                                 expected_uid=None)
            self.assertEqual(caught.exception.code, "worker_network_boundary")

    def test_rejects_unlisted_environment_name_before_browser_start(self):
        from smartfetch.browser_prototype.worker import WorkerBoundaryError, inspect_boundary

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "interfaces").mkdir()
            (root / "interfaces" / "lo").touch()
            (root / "fd").mkdir()
            (root / "route").write_text("Iface Destination Gateway\n", encoding="ascii")
            with self.assertRaises(WorkerBoundaryError) as caught:
                inspect_boundary(interfaces=root / "interfaces", fds=root / "fd",
                                 routes=root / "route",
                                 environment={"PATH": "/usr/bin", "SYNTHETIC_SECRET": "marker"},
                                 expected_uid=None)
            self.assertEqual(caught.exception.code, "worker_environment")


if __name__ == "__main__":
    unittest.main()
