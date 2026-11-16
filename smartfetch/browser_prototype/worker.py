"""Disabled local Linux browser worker; never imported by production dispatch.

The trusted launcher must create a separate no-upstream network namespace and
pass only a bounded Unix IPC endpoint. This module checks that boundary again
before it starts any relay or Chromium process. It does not create a namespace
or silently weaken one when the host cannot provide it.
"""

import os
from pathlib import Path
import time


ALLOWED_ENVIRONMENT_NAMES = frozenset({
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE",
})


class WorkerBoundaryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def inspect_boundary(*, interfaces: Path = Path("/sys/class/net"),
                     fds: Path = Path("/proc/self/fd"),
                     routes: Path = Path("/proc/net/route"),
                     environment: dict[str, str] | None = None,
                     expected_uid: int | None = 10001) -> None:
    """Fail closed on unexpected UID, interface, or inherited descriptor.

    The interface check is a readback, not a way of creating or enforcing the
    namespace. A separate hostile-socket test must prove kernel denial.
    """
    if expected_uid is not None:
        if not hasattr(os, "geteuid") or expected_uid == 0 or os.geteuid() != expected_uid:
            raise WorkerBoundaryError("worker_uid")
    try:
        names = set(os.environ if environment is None else environment)
        if names - ALLOWED_ENVIRONMENT_NAMES:
            raise WorkerBoundaryError("worker_environment")
        if set(os.listdir(interfaces)) != {"lo"}:
            raise WorkerBoundaryError("worker_network_boundary")
        lines = routes.read_text(encoding="ascii").splitlines()
        if not lines or not lines[0].startswith("Iface"):
            raise WorkerBoundaryError("worker_boundary_unverifiable")
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 8:
                raise WorkerBoundaryError("worker_boundary_unverifiable")
            if fields[0] != "lo" or fields[1] == "00000000":
                raise WorkerBoundaryError("worker_network_boundary")
        for entry in os.listdir(fds):
            if not entry.isdecimal() or int(entry) <= 2:
                continue
            try:
                os.readlink(fds / entry)
            except FileNotFoundError:
                # /proc enumeration can include its own already-closed FD.
                continue
            raise WorkerBoundaryError("worker_inherited_fd")
    except WorkerBoundaryError:
        raise
    except (OSError, ValueError) as error:
        raise WorkerBoundaryError("worker_boundary_unverifiable") from error


def render_in_isolated_worker(url: str, *, socket_path: str,
                              operation_id: bytes, chromium: str,
                              deadline: float, max_chars: int) -> dict:
    """Run the local renderer only after the no-upstream preflight succeeds."""
    inspect_boundary()
    if time.monotonic() >= deadline:
        raise WorkerBoundaryError("worker_timeout")
    from .harness import render_local
    from .relay import WorkerRelay

    relay = WorkerRelay(socket_path, operation_id=operation_id,
                        deadline=deadline)
    failure = None
    result = None
    try:
        address = relay.start()
        result = render_local(url, chromium=chromium,
                              proxy_host=address[0], proxy_port=address[1],
                              deadline=deadline, max_chars=max_chars)
    except Exception as error:
        failure = error
    finally:
        if not relay.close():
            failure = WorkerBoundaryError("worker_relay_cleanup_uncertain")
    if failure is not None:
        raise failure
    return result
