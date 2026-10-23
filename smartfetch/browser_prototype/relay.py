"""Bounded Unix-stream framing for a disabled, credential-free browser worker."""

import hmac
import os
from pathlib import Path
import select
import socket
import stat
import struct
import threading
import time


MAX_FRAME_BYTES = 8192


class RelayError(RuntimeError):
    """Finite code suitable for private local test evidence."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _set_nonblocking(sock) -> None:
    try:
        sock.setblocking(False)
    except OSError as error:
        raise RelayError("relay_io") from error


def _wait(sock, *, writable: bool, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RelayError("relay_timeout")
    try:
        readable, ready_to_write, _ = select.select(
            [] if writable else [sock], [sock] if writable else [], [], remaining,
        )
    except (OSError, ValueError) as error:
        raise RelayError("relay_io") from error
    if not (ready_to_write if writable else readable):
        raise RelayError("relay_timeout")


def _recv_exact(sock, size: int, *, deadline: float) -> bytes:
    value = bytearray()
    _set_nonblocking(sock)
    while len(value) < size:
        _wait(sock, writable=False, deadline=deadline)
        try:
            chunk = sock.recv(size - len(value))
        except BlockingIOError:
            continue
        except OSError as error:
            raise RelayError("relay_io") from error
        if not chunk:
            raise RelayError("relay_eof")
        value.extend(chunk)
    return bytes(value)


def _send_all(sock, data: bytes, *, deadline: float) -> None:
    sent = 0
    _set_nonblocking(sock)
    while sent < len(data):
        _wait(sock, writable=True, deadline=deadline)
        try:
            count = sock.send(data[sent:])
        except BlockingIOError:
            continue
        except OSError as error:
            raise RelayError("relay_io") from error
        if count <= 0:
            raise RelayError("relay_eof")
        sent += count


def send_frame(sock, data: bytes, *, deadline: float) -> None:
    if not isinstance(data, bytes) or len(data) > MAX_FRAME_BYTES:
        raise RelayError("frame_too_large")
    _send_all(sock, struct.pack("!I", len(data)) + data, deadline=deadline)


def recv_frame(sock, *, deadline: float) -> bytes:
    size = struct.unpack("!I", _recv_exact(sock, 4, deadline=deadline))[0]
    if size > MAX_FRAME_BYTES:
        raise RelayError("frame_too_large")
    return _recv_exact(sock, size, deadline=deadline) if size else b""


def _recv_plain(sock, *, deadline: float) -> bytes:
    _set_nonblocking(sock)
    while True:
        _wait(sock, writable=False, deadline=deadline)
        try:
            return sock.recv(MAX_FRAME_BYTES)
        except BlockingIOError:
            continue
        except OSError as error:
            raise RelayError("relay_io") from error


def _duplex(plain, framed, *, deadline: float, max_bytes: int) -> None:
    closing = threading.Event()
    budget_lock = threading.Lock()
    used = 0

    def reserve(size):
        nonlocal used
        with budget_lock:
            if used + size > max_bytes:
                raise RelayError("relay_budget")
            used += size

    def plain_to_framed():
        try:
            while not closing.is_set():
                data = _recv_plain(plain, deadline=deadline)
                reserve(len(data))
                send_frame(framed, data, deadline=deadline)
                if not data:
                    return
        except RelayError:
            closing.set()

    def framed_to_plain():
        try:
            while not closing.is_set():
                data = recv_frame(framed, deadline=deadline)
                if not data:
                    try:
                        plain.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                reserve(len(data))
                _send_all(plain, data, deadline=deadline)
        except RelayError:
            closing.set()

    sender = threading.Thread(target=plain_to_framed, daemon=True)
    receiver = threading.Thread(target=framed_to_plain, daemon=True)
    sender.start()
    receiver.start()
    while (sender.is_alive() or receiver.is_alive()) and time.monotonic() < deadline:
        if closing.is_set():
            for sock in (plain, framed):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        sender.join(timeout=min(0.05, max(0, deadline - time.monotonic())))
        receiver.join(timeout=0)
    if sender.is_alive() or receiver.is_alive():
        raise RelayError("relay_cleanup_uncertain")


class _RelayServer:
    def __init__(self, *, deadline: float, max_bytes: int = 4_000_000):
        self.deadline = deadline
        self.max_bytes = max_bytes
        self._listener = None
        self._thread = None
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._sessions = set()
        self._sockets = set()
        self.failures = 0

    def _serve(self):
        while not self._closing.is_set() and time.monotonic() < self.deadline:
            try:
                ready, _, _ = select.select([self._listener], [], [], 0.05)
                if not ready:
                    continue
                client, _ = self._listener.accept()
            except (OSError, ValueError):
                if not self._closing.is_set():
                    self.failures += 1
                break
            with self._lock:
                self._sockets.add(client)
            thread = threading.Thread(target=self._handle_and_release,
                                      args=(client,), daemon=True)
            with self._lock:
                self._sessions.add(thread)
            thread.start()

    def _handle_and_release(self, client):
        try:
            self._handle(client)
        except (OSError, RelayError):
            self.failures += 1
        finally:
            with self._lock:
                self._sockets.discard(client)
                self._sessions.discard(threading.current_thread())
            client.close()

    def _start(self, listener):
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> bool:
        if self._listener is None:
            return True
        cleanup_deadline = min(self.deadline, time.monotonic() + 3)
        self._closing.set()
        with self._lock:
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self._listener.close()
        self._thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        while time.monotonic() < cleanup_deadline:
            with self._lock:
                sessions = tuple(self._sessions)
            if not sessions:
                break
            for thread in sessions:
                thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        with self._lock:
            return not self._thread.is_alive() and not self._sessions and not self._sockets


class BridgeServer(_RelayServer):
    """Trusted Unix listener; the only TCP peer is the validated broker."""

    def __init__(self, socket_path: str, broker_address: tuple[str, int], *,
                 operation_id: bytes, deadline: float, allowed_uid: int | None = None):
        super().__init__(deadline=deadline)
        if len(operation_id) != 16 or broker_address[0] != "127.0.0.1":
            raise ValueError("invalid_relay_boundary")
        self.socket_path = socket_path
        self.broker_address = broker_address
        self.operation_id = operation_id
        self.allowed_uid = allowed_uid
        self._socket_inode = None
        self.denied_handshakes = 0

    def start(self):
        if self._listener is not None or Path(self.socket_path).exists():
            raise RuntimeError("bridge_already_started")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(self.socket_path)
            self._socket_inode = os.stat(self.socket_path).st_ino
            os.chmod(self.socket_path, 0o600)
            listener.listen(16)
            self._start(listener)
        except Exception:
            listener.close()
            if self._socket_inode is not None:
                try:
                    current = os.stat(self.socket_path)
                    if current.st_ino != self._socket_inode or not stat.S_ISSOCK(current.st_mode):
                        raise RelayError("relay_cleanup_uncertain")
                    os.unlink(self.socket_path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise RelayError("relay_cleanup_uncertain") from error
            raise
        return self.socket_path

    def _handle(self, client):
        if self.allowed_uid is not None:
            if not hasattr(socket, "SO_PEERCRED"):
                self.denied_handshakes += 1
                return
            credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != self.allowed_uid:
                self.denied_handshakes += 1
                return
        try:
            hello = recv_frame(client, deadline=self.deadline)
        except RelayError:
            self.denied_handshakes += 1
            return
        if not hmac.compare_digest(hello, b"SFBR1" + self.operation_id):
            self.denied_handshakes += 1
            return
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RelayError("relay_timeout")
        upstream = socket.create_connection(self.broker_address, timeout=remaining)
        with self._lock:
            self._sockets.add(upstream)
        try:
            _duplex(upstream, client, deadline=self.deadline,
                    max_bytes=self.max_bytes)
        finally:
            with self._lock:
                self._sockets.discard(upstream)
            upstream.close()

    def close(self) -> bool:
        stopped = super().close()
        try:
            current = os.stat(self.socket_path)
        except FileNotFoundError:
            return stopped
        if current.st_ino != self._socket_inode or not stat.S_ISSOCK(current.st_mode):
            return False
        os.unlink(self.socket_path)
        return stopped and not Path(self.socket_path).exists()


class WorkerRelay(_RelayServer):
    """Worker loopback proxy; outbound connections are AF_UNIX only."""

    def __init__(self, socket_path: str, *, operation_id: bytes,
                 deadline: float):
        super().__init__(deadline=deadline)
        if len(operation_id) != 16:
            raise ValueError("invalid_relay_boundary")
        self.socket_path = socket_path
        self.operation_id = operation_id

    def start(self):
        if self._listener is not None:
            raise RuntimeError("relay_already_started")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        self._start(listener)
        return listener.getsockname()

    def _handle(self, client):
        ipc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with self._lock:
            self._sockets.add(ipc)
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RelayError("relay_timeout")
            ipc.settimeout(remaining)
            ipc.connect(self.socket_path)
            send_frame(ipc, b"SFBR1" + self.operation_id,
                       deadline=self.deadline)
            _duplex(client, ipc, deadline=self.deadline,
                    max_bytes=self.max_bytes)
        finally:
            with self._lock:
                self._sockets.discard(ipc)
            ipc.close()
