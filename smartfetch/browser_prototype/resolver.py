"""Bounded child-process resolver for the disabled browser prototype."""

import json
import os
import subprocess
import sys
import time


_RESOLVE_CODE = """\
import json, socket, sys
host, port = sys.argv[1], int(sys.argv[2])
answers = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(
    host, port, type=socket.SOCK_STREAM)))
if not answers or len(answers) > 16:
    raise SystemExit(2)
payload = json.dumps(answers, separators=(',', ':'))
if len(payload) > 2048:
    raise SystemExit(2)
sys.stdout.write(payload)
"""


def run_bounded_child(argv: list[str], *, deadline: float) -> bytes:
    """Run one credential-free child, reserving 250 ms for kill/readback.

    This bounds a libc resolver stall in the child; a failed kill/readback
    raises rather than reporting a clean DNS failure.
    """
    wait_seconds = deadline - time.monotonic() - 0.25
    if wait_seconds <= 0:
        raise TimeoutError("resolver_deadline")
    environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR")
                   if name in os.environ}
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=environment, close_fds=True,
    )
    try:
        try:
            output, _ = process.communicate(timeout=max(0, deadline - time.monotonic() - 0.25))
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("resolver_cleanup_uncertain") from error
            raise TimeoutError("resolver_deadline") from None
        if process.returncode != 0 or len(output) > 4096:
            raise OSError("resolver_failed")
        return output
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("resolver_cleanup_uncertain") from error


def resolve_system(host: str, port: int, *, deadline: float) -> list[str]:
    """Resolve a validated hostname without an unkillable parent getaddrinfo."""
    output = run_bounded_child(
        [sys.executable, "-I", "-c", _RESOLVE_CODE, host, str(port)],
        deadline=deadline,
    )
    try:
        answers = json.loads(output)
    except (UnicodeError, ValueError) as error:
        raise OSError("resolver_failed") from error
    if (not isinstance(answers, list) or not 1 <= len(answers) <= 16 or
            not all(isinstance(answer, str) and len(answer) <= 64
                    for answer in answers)):
        raise OSError("resolver_failed")
    return answers
