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


def _wait(sock, *, writable: bool, deadline: float, cancelled=None) -> None:
    while True:
        if cancelled is not None and cancelled.is_set():
            raise RelayError("relay_cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayError("relay_timeout")
        try:
            readable, ready_to_write, _ = select.select(
                [] if writable else [sock], [sock] if writable else [], [],
                min(remaining, 0.05),
            )
        except (OSError, ValueError) as error:
            raise RelayError("relay_io") from error
        if ready_to_write if writable else readable:
            return


def _recv_exact(sock, size: int, *, deadline: float, cancelled=None, budget=None) -> bytes:
    value = bytearray()
    _set_nonblocking(sock)
    while len(value) < size:
        _wait(sock, writable=False, deadline=deadline, cancelled=cancelled)
        allowance = size - len(value)
        if budget is not None:
            allowance = budget._claim_read(allowance)
        try:
            chunk = sock.recv(allowance)
        except BlockingIOError:
            if budget is not None:
                budget._refund_bytes(allowance)
            continue
        except OSError as error:
            if budget is not None:
                budget._refund_bytes(allowance)
            raise RelayError("relay_io") from error
        if budget is not None:
            budget._refund_bytes(allowance - len(chunk))
        if not chunk:
            raise RelayError("relay_eof")
        value.extend(chunk)
    return bytes(value)


def _send_all(sock, data: bytes, *, deadline: float, cancelled=None) -> None:
    sent = 0
    _set_nonblocking(sock)
    while sent < len(data):
        _wait(sock, writable=True, deadline=deadline, cancelled=cancelled)
        try:
            count = sock.send(data[sent:])
        except BlockingIOError:
            continue
        except OSError as error:
            raise RelayError("relay_io") from error
        if count <= 0:
            raise RelayError("relay_eof")
        sent += count


def send_frame(sock, data: bytes, *, deadline: float, cancelled=None) -> None:
    if not isinstance(data, bytes) or len(data) > MAX_FRAME_BYTES:
        raise RelayError("frame_too_large")
    _send_all(sock, struct.pack("!I", len(data)) + data, deadline=deadline,
              cancelled=cancelled)


def recv_frame(sock, *, deadline: float, cancelled=None, budget=None) -> bytes:
    size = struct.unpack("!I", _recv_exact(sock, 4, deadline=deadline,
                                         cancelled=cancelled, budget=budget))[0]
    if size > MAX_FRAME_BYTES:
        raise RelayError("frame_too_large")
    return _recv_exact(sock, size, deadline=deadline, cancelled=cancelled,
                       budget=budget) if size else b""


def _recv_plain(sock, *, deadline: float, cancelled=None) -> bytes:
    _set_nonblocking(sock)
    while True:
        _wait(sock, writable=False, deadline=deadline, cancelled=cancelled)
        try:
            return sock.recv(MAX_FRAME_BYTES)
        except BlockingIOError:
            continue
        except OSError as error:
            raise RelayError("relay_io") from error


def _duplex(plain, framed, *, deadline: float, reserve, owner=None) -> None:
    closing = threading.Event()

    def plain_to_framed():
        try:
            while not closing.is_set():
                data = _recv_plain(plain, deadline=deadline, cancelled=closing)
                reserve(len(data) + 4)
                send_frame(framed, data, deadline=deadline, cancelled=closing)
                if not data:
                    return
        except RelayError:
            closing.set()

    def framed_to_plain():
        try:
            while not closing.is_set():
                data = recv_frame(framed, deadline=deadline, cancelled=closing,
                                  budget=owner)
                if owner is None:
                    reserve(len(data) + 4)
                if not data:
                    try:
                        plain.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                _send_all(plain, data, deadline=deadline, cancelled=closing)
        except RelayError:
            closing.set()

    started = []
    try:
        for target in (plain_to_framed, framed_to_plain):
            def owned_target(target=target):
                try:
                    target()
                finally:
                    if owner is not None:
                        with owner._lock:
                            owner._child_threads.discard(threading.current_thread())

            thread = threading.Thread(target=owned_target, daemon=True)
            if owner is not None:
                with owner._lock:
                    owner._child_threads.add(thread)
                    try:
                        thread.start()
                    except Exception:
                        owner._child_threads.discard(thread)
                        raise
            else:
                thread.start()
            started.append(thread)
    except Exception as error:
        closing.set()
        for sock in (plain, framed):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in started:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in started):
            raise RelayError("relay_cleanup_uncertain") from error
        raise RelayError("relay_start") from error
    sender, receiver = started
    while (sender.is_alive() or receiver.is_alive()) and time.monotonic() < deadline:
        if closing.is_set():
            for sock in (plain, framed):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        sender.join(timeout=min(0.05, max(0, deadline - time.monotonic())))
        receiver.join(timeout=0)
    if closing.is_set():
        # Both pumps may finish before the coordinator observes cancellation.
        # Still close their transport directions before reporting completion.
        for sock in (plain, framed):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    if sender.is_alive() or receiver.is_alive():
        raise RelayError("relay_cleanup_uncertain")


class _RelayServer:
    def __init__(self, *, deadline: float, max_bytes: int = 4_000_000,
                 max_sessions: int = 16, max_total_sessions: int = 32):
        if min(max_bytes, max_sessions, max_total_sessions) < 1:
            raise ValueError("invalid_relay_limit")
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.max_sessions = max_sessions
        self.max_total_sessions = max_total_sessions
        self.admitted_sessions = 0
        self.operation_bytes = 0
        self._listener = None
        self._thread = None
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._sessions = set()
        self._child_threads = set()
        self._sockets = set()
        self._cleanup_uncertain = False
        self.failures = 0

    def _reserve_bytes(self, size):
        with self._lock:
            if not self._closing.is_set() and self.operation_bytes + size <= self.max_bytes:
                self.operation_bytes += size
                return
            self._closing.set()
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        raise RelayError("relay_budget")

    def _claim_read(self, maximum):
        with self._lock:
            allowance = min(maximum, self.max_bytes - self.operation_bytes)
            if allowance > 0 and not self._closing.is_set():
                self.operation_bytes += allowance
                return allowance
            self._closing.set()
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        raise RelayError("relay_budget")

    def _refund_bytes(self, size):
        with self._lock:
            self.operation_bytes -= size

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
            thread = None
            try:
                with self._lock:
                    if (self._closing.is_set() or len(self._sessions) >= self.max_sessions or
                            self.admitted_sessions >= self.max_total_sessions):
                        client.close()
                        continue
                    self.admitted_sessions += 1
                    self._sockets.add(client)
                    thread = threading.Thread(target=self._handle_and_release,
                                              args=(client,), daemon=True)
                    self._sessions.add(thread)
                    thread.start()
            except Exception:
                with self._lock:
                    self._sessions.discard(thread)
                    self._sockets.discard(client)
                    self.failures += 1
                client.close()
                self._closing.set()
                return

    def _handle_and_release(self, client):
        try:
            self._handle(client)
        except (OSError, RelayError) as error:
            with self._lock:
                self.failures += 1
                if isinstance(error, RelayError) and error.code == "relay_cleanup_uncertain":
                    self._cleanup_uncertain = True
        finally:
            with self._lock:
                self._sockets.discard(client)
                self._sessions.discard(threading.current_thread())
            client.close()

    def _start(self, listener):
        self._listener = listener
        try:
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
        except Exception:
            listener.close()
            self._listener = None
            self._thread = None
            raise

    def close(self) -> bool:
        if self._listener is None:
            return not self._cleanup_uncertain and not self._child_threads
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
                sessions = tuple(self._sessions | self._child_threads)
            if not sessions:
                break
            for thread in sessions:
                thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        with self._lock:
            return (not self._thread.is_alive() and not self._sessions and
                    not self._child_threads and not self._sockets and
                    not self._cleanup_uncertain)


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
            hello = recv_frame(client, deadline=self.deadline, budget=self)
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
                    reserve=self._reserve_bytes, owner=self)
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
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(self.max_sessions)
            self._start(listener)
        except Exception:
            listener.close()
            raise
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
            self._reserve_bytes(len(b"SFBR1" + self.operation_id) + 4)
            send_frame(ipc, b"SFBR1" + self.operation_id,
                       deadline=self.deadline)
            _duplex(client, ipc, deadline=self.deadline,
                    reserve=self._reserve_bytes, owner=self)
        finally:
            with self._lock:
                self._sockets.discard(ipc)
            ipc.close()


class DynamicWorkerRelay(_RelayServer):
    """Worker loopback proxy for persistent warm container whose active job socket changes per request."""

    def __init__(
        self,
        *,
        deadline: float = float("inf"),
        max_bytes: int = 10_000_000,
        max_sessions: int = 16,
        max_total_sessions: int = 100_000,
    ):
        super().__init__(
            deadline=deadline,
            max_bytes=max_bytes,
            max_sessions=max_sessions,
            max_total_sessions=max_total_sessions,
        )
        self._active_job: tuple[str, bytes, float] | None = None
        self._job_lock = threading.Lock()
        self._active_upstreams: set[socket.socket] = set()

    def start(self) -> tuple[str, int]:
        if self._listener is not None:
            raise RuntimeError("relay_already_started")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(self.max_sessions)
            self._start(listener)
        except Exception:
            listener.close()
            raise
        return listener.getsockname()

    def set_active_job(self, socket_path: str, operation_id: bytes, deadline: float) -> None:
        if len(operation_id) != 16:
            raise ValueError("invalid_relay_boundary")
        self.clear_active_job()
        with self._job_lock:
            self._active_job = (socket_path, operation_id, deadline)
        with self._lock:
            self.operation_bytes = 0
            self.admitted_sessions = 0

    def clear_active_job(self) -> None:
        with self._job_lock:
            self._active_job = None
            upstreams = list(self._active_upstreams)
            self._active_upstreams.clear()
        for sock in upstreams:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, client):
        with self._job_lock:
            job = self._active_job
        if job is None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
            return

        socket_path, operation_id, job_deadline = job
        remaining = job_deadline - time.monotonic()
        if remaining <= 0:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
            return

        af_unix = getattr(socket, "AF_UNIX", None)
        if af_unix is None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
            raise RelayError("relay_af_unix_unavailable")

        ipc = socket.socket(af_unix, socket.SOCK_STREAM)
        with self._job_lock:
            if self._active_job is None:
                ipc.close()
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
                return
            self._active_upstreams.add(ipc)
        with self._lock:
            self._sockets.add(ipc)
        try:
            ipc.settimeout(remaining)
            ipc.connect(socket_path)
            self._reserve_bytes(len(b"SFBR1" + operation_id) + 4)
            send_frame(ipc, b"SFBR1" + operation_id, deadline=job_deadline)
            _duplex(
                client,
                ipc,
                deadline=job_deadline,
                reserve=self._reserve_bytes,
                owner=self,
            )
        finally:
            with self._job_lock:
                self._active_upstreams.discard(ipc)
            with self._lock:
                self._sockets.discard(ipc)
            try:
                ipc.close()
            except OSError:
                pass

    def close(self) -> bool:
        self.clear_active_job()
        return super().close()
