"""Local-only bounded proxy broker; deliberately not wired into the service."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from .policy import DestinationPolicy, PolicyError


def _system_resolver(host: str, port: int) -> list[str]:
    return list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )))


def _system_connector(ip: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=timeout)


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, _request, _client_address):
        # Socket closures and unexpected handler failures are finite counters,
        # never raw tracebacks containing browser-controlled request material.
        self.broker._count("handler_failures")

    def process_request(self, request, client_address):
        if not self.broker._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
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
            self.broker._slots.release()


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
        header_bytes = len(self.raw_requestline) + sum(
            len(name) + len(value) + 4 for name, value in self.headers.items()
        )
        if header_bytes > broker.max_request_bytes:
            self._reply(431)
            return None
        try:
            target, ip = broker.policy.resolve_target(
                self.path, connect=connect, resolver=broker.resolver
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
                    streams[1 - index].sendall(chunk)
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
        except TimeoutError:
            self._reply(503)
        except OSError:
            self._reply(502)
        else:
            try:
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
                 max_connections: int = 16):
        if (max_request_bytes < 256 or max_tunnel_bytes_each_direction < 1 or
                max_connections < 1):
            raise ValueError("invalid broker limit")
        self.policy = policy or DestinationPolicy()
        self.resolver = resolver or _system_resolver
        self.connector = connector or _system_connector
        self.total_deadline = total_deadline
        self.max_request_bytes = max_request_bytes
        self.max_tunnel_bytes_each_direction = max_tunnel_bytes_each_direction
        self._slots = threading.BoundedSemaphore(max_connections)
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._active = set()
        self._threads = set()
        self.allowed = 0
        self.denied = 0
        self.connects = 0
        self.handler_failures = 0
        self._server = None
        self._serve_thread = None

    def _count(self, field: str):
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

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
        self._closing.set()
        with self._lock:
            active = tuple(self._active)
            threads = tuple(self._threads)
        for sock in active:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self._server.shutdown()
        self._server.server_close()
        self._serve_thread.join(timeout=1)
        for thread in threads:
            thread.join(timeout=1)
        with self._lock:
            return (not self._serve_thread.is_alive() and
                    not any(thread.is_alive() for thread in self._threads) and
                    not self._active)
