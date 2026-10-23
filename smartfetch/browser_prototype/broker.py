"""Local-only bounded proxy broker; deliberately not wired into the service."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from .policy import DestinationPolicy, PolicyError
from .resolver import resolve_system


def _system_resolver(host: str, port: int, *, deadline: float) -> list[str]:
    return resolve_system(host, port, deadline=deadline)


def _system_connector(ip: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=timeout)


class _HeaderLimit(Exception):
    def __init__(self, status):
        self.status = status


class _HeaderReader:
    """Bound raw bytes and the whole header clock before stdlib parsing.

    Read exactly through each newline so CONNECT payload never enters a
    buffered reader. Budget admission happens before each socket byte.
    """

    def __init__(self, handler):
        self.handler = handler
        self.used = 0
        self.deadline = min(handler.broker.total_deadline, time.monotonic() + 0.25)

    def readline(self, size=-1):
        broker = self.handler.broker
        value = bytearray()
        limit = broker.max_request_bytes if size < 0 else size
        while len(value) < limit:
            if self.used >= broker.max_request_bytes:
                raise _HeaderLimit(431)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if not broker._reserve_bytes(1):
                raise _HeaderLimit(429)
            try:
                self.handler.connection.settimeout(remaining)
                chunk = self.handler.connection.recv(1)
            except OSError:
                broker._refund_byte()
                raise
            if not chunk:
                broker._refund_byte()
                break
            self.used += 1
            value.extend(chunk)
            if chunk == b"\n":
                break
        return bytes(value)


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, _request, _client_address):
        # Socket closures and unexpected handler failures are finite counters,
        # never raw tracebacks containing browser-controlled request material.
        self.broker._count("handler_failures")

    def process_request(self, request, client_address):
        if self.broker._closing.is_set():
            self.shutdown_request(request)
            return
        if time.monotonic() >= self.broker.total_deadline:
            # No HTTP request has been admitted. Closing unread TCP bytes can
            # reset the connection, so no HTTP response is promised here.
            self.shutdown_request(request)
            return
        if not self.broker._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        request.settimeout(min(0.25, max(0.01, self.broker.total_deadline - time.monotonic())))
        with self.broker._lock:
            self.broker._clients.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self.broker._lock:
                self.broker._clients.discard(request)
            self.broker._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        current = threading.current_thread()
        with self.broker._lock:
            self.broker._threads.add(current)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.broker._lock:
                self.broker._threads.discard(current)
                self.broker._clients.discard(request)
            self.broker._slots.release()


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        self.request_version = "HTTP/1.1"
        self.requestline = ""
        self.command = None
        if time.monotonic() >= self.broker.total_deadline:
            self._reply(503)
            return
        if not self.broker._reserve_request(0):
            self._reply(429)
            return
        original = self.rfile
        self.rfile = _HeaderReader(self)
        try:
            super().handle_one_request()
        except _HeaderLimit as error:
            self._reply(error.status)
        finally:
            self.rfile = original

    def send_error(self, code, message=None, explain=None):
        # Never reflect an unsupported method or parser error in an HTML body.
        self._reply(405 if code == 501 else code)

    @property
    def broker(self):
        return self.server.broker

    def log_message(self, *_args):
        pass

    def _reply(self, status: int):
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            # A cancelled browser may already have closed its local socket.
            pass
        finally:
            self.close_connection = True

    def _admit(self, *, connect: bool):
        broker = self.broker
        if broker._closing.is_set() or time.monotonic() >= broker.total_deadline:
            self._reply(503)
            return None
        try:
            def bounded_default_resolver(host, port):
                try:
                    return _system_resolver(host, port,
                                            deadline=broker.total_deadline)
                except RuntimeError as error:
                    if str(error) == "resolver_cleanup_uncertain":
                        broker._cleanup_uncertain = True
                    raise

            resolver = broker.resolver or bounded_default_resolver
            target, ip = broker.policy.resolve_target(
                self.path, connect=connect, resolver=resolver
            )
        except PolicyError:
            broker._count("denied")
            self._reply(403)
            return None
        upstream = None
        try:
            remaining = broker.total_deadline - time.monotonic()
            if remaining <= 0:
                self._reply(503)
                return None
            upstream = broker.connector(ip, target.port, min(remaining, 2.0))
            peer = upstream.getpeername()[0]
            broker.policy.verify_peer(ip, peer)
            upstream.settimeout(min(remaining, 2.0))
        except PolicyError:
            if upstream is not None:
                upstream.close()
            broker._count("denied")
            self._reply(403)
            return None
        except (OSError, TimeoutError, AttributeError):
            if upstream is not None:
                upstream.close()
            broker._count("denied")
            self._reply(502)
            return None
        broker._register(upstream)
        broker._count("allowed")
        return target, upstream

    def do_CONNECT(self):
        admitted = self._admit(connect=True)
        if admitted is None:
            return
        _, upstream = admitted
        self.broker._count("connects")
        try:
            remaining = self.broker.total_deadline - time.monotonic()
            if remaining <= 0:
                return
            self.connection.settimeout(remaining)
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            streams = (self.connection, upstream)
            totals = [0, 0]
            while not self.broker._closing.is_set():
                remaining = self.broker.total_deadline - time.monotonic()
                if remaining <= 0:
                    break
                readable, _, _ = select.select(streams, (), (), min(remaining, 0.1))
                for source in readable:
                    index = 0 if source is streams[0] else 1
                    chunk = source.recv(8192)
                    if not chunk:
                        return
                    totals[index] += len(chunk)
                    if totals[index] > self.broker.max_tunnel_bytes_each_direction:
                        return
                    if not self.broker._reserve_bytes(len(chunk)):
                        return
                    remaining = self.broker.total_deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    destination = streams[1 - index]
                    destination.settimeout(remaining)
                    destination.sendall(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.broker._unregister(upstream)
            upstream.close()
            self.close_connection = True

    def do_GET(self):
        try:
            scheme = urlsplit(self.path).scheme.lower()
        except ValueError:
            scheme = ""
        if scheme != "http":
            self.broker._count("denied")
            self._reply(403)
            return
        admitted = self._admit(connect=False)
        if admitted is None:
            return
        target, upstream = admitted
        try:
            parsed = urlsplit(self.path)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            host = target.host
            if target.port != 80:
                host += f":{target.port}"
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            if len(request) > self.broker.max_request_bytes:
                self._reply(431)
                return
            if not self.broker._reserve_bytes(len(request)):
                self._reply(502)
                return
            remaining = self.broker.total_deadline - time.monotonic()
            if remaining <= 0:
                self._reply(503)
                return
            upstream.settimeout(remaining)
            upstream.sendall(request)
            response = bytearray()
            while not self.broker._closing.is_set():
                remaining = self.broker.total_deadline - time.monotonic()
                if remaining <= 0:
                    self._reply(503)
                    return
                upstream.settimeout(min(remaining, 1.0))
                chunk = upstream.recv(8192)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > self.broker.max_tunnel_bytes_each_direction:
                    self._reply(502)
                    return
                if not self.broker._reserve_bytes(len(chunk)):
                    self._reply(502)
                    return
        except TimeoutError:
            self._reply(503)
        except OSError:
            self._reply(502)
        else:
            try:
                remaining = self.broker.total_deadline - time.monotonic()
                if remaining <= 0:
                    self._reply(503)
                    return
                self.connection.settimeout(remaining)
                self.connection.sendall(response)
            except OSError:
                pass
        finally:
            self.broker._unregister(upstream)
            upstream.close()
            self.close_connection = True

    def do_HEAD(self):
        self._reply(405)

    def do_POST(self):
        self._reply(405)


class Broker:
    """A bounded local proxy; caller must supply an isolated worker runtime."""

    def __init__(self, policy: DestinationPolicy | None = None,
                 resolver=None, connector=None, *, total_deadline: float,
                 max_request_bytes: int = 8192,
                 max_tunnel_bytes_each_direction: int = 2_000_000,
                 max_connections: int = 16, max_requests: int = 32,
                 max_operation_bytes: int = 4_000_000):
        if (max_request_bytes < 256 or max_tunnel_bytes_each_direction < 1 or
                max_connections < 1 or max_requests < 1 or
                max_operation_bytes < 1):
            raise ValueError("invalid broker limit")
        self.policy = policy or DestinationPolicy()
        self.resolver = resolver
        self.connector = connector or _system_connector
        self.total_deadline = total_deadline
        self.max_request_bytes = max_request_bytes
        self.max_tunnel_bytes_each_direction = max_tunnel_bytes_each_direction
        self.max_requests = max_requests
        self.max_operation_bytes = max_operation_bytes
        self._slots = threading.BoundedSemaphore(max_connections)
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._active = set()
        self._clients = set()
        self._threads = set()
        self.allowed = 0
        self.denied = 0
        self.connects = 0
        self.handler_failures = 0
        self._cleanup_uncertain = False
        self.operation_requests = 0
        self.operation_bytes = 0
        self._server = None
        self._serve_thread = None

    def _count(self, field: str):
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    def _reserve_request(self, size: int) -> bool:
        with self._lock:
            if (self.operation_requests >= self.max_requests or
                    self.operation_bytes + size > self.max_operation_bytes):
                return False
            self.operation_requests += 1
            self.operation_bytes += size
            return True

    def _reserve_bytes(self, size: int) -> bool:
        with self._lock:
            if self.operation_bytes + size > self.max_operation_bytes:
                return False
            self.operation_bytes += size
            return True

    def _refund_byte(self):
        with self._lock:
            self.operation_bytes -= 1

    def _register(self, sock):
        with self._lock:
            self._active.add(sock)

    def _unregister(self, sock):
        with self._lock:
            self._active.discard(sock)

    def start(self):
        if self._server is not None:
            raise RuntimeError("broker already started")
        self._server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler)
        self._server.broker = self
        self._serve_thread = threading.Thread(target=self._server.serve_forever,
                                              kwargs={"poll_interval": 0.05},
                                              name="browser-prototype-broker",
                                              daemon=True)
        self._serve_thread.start()
        return self._server.server_address

    def close(self):
        if self._server is None:
            return True
        cleanup_deadline = min(self.total_deadline, time.monotonic() + 3.0)
        self._closing.set()
        with self._lock:
            active = tuple(self._active)
            clients = tuple(self._clients)
        for sock in active + clients:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self._server.shutdown()
        self._server.server_close()
        self._serve_thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        while time.monotonic() < cleanup_deadline:
            with self._lock:
                threads = tuple(self._threads)
            if not threads:
                break
            for thread in threads:
                thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        with self._lock:
            return (not self._serve_thread.is_alive() and
                    not self._threads and not self._active and
                    not self._clients and not self._cleanup_uncertain)
