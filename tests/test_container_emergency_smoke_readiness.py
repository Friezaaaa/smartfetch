"""The container smoke waits for its loopback listener, but not for failures."""

import importlib.util
from pathlib import Path
import time
from types import SimpleNamespace
import urllib.error

import pytest


_SCRIPT = Path(__file__).with_name("container_emergency_smoke.py")
_SPEC = importlib.util.spec_from_file_location("container_emergency_smoke", _SCRIPT)
assert _SPEC and _SPEC.loader
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


def test_waits_for_transient_loopback_connection_refusal(monkeypatch):
    attempts = 0

    def request(base, path):
        nonlocal attempts
        assert base == "http://127.0.0.1:8787" and path == "/health"
        attempts += 1
        if attempts < 3:
            raise urllib.error.URLError("listener not ready")
        return 200, {}, b'{"version":"1.11.1"}'

    monkeypatch.setattr(smoke, "request", request)
    assert smoke.wait_for_health("http://127.0.0.1:8787", timeout_seconds=1) == (
        200, {}, b'{"version":"1.11.1"}'
    )
    assert attempts == 3


def test_readiness_wait_has_finite_deadline(monkeypatch):
    def request(_base, _path):
        raise urllib.error.URLError("listener not ready")

    monkeypatch.setattr(smoke, "request", request)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        smoke.wait_for_health("http://127.0.0.1:8787", timeout_seconds=0.05)
    assert time.monotonic() - started < 1


def test_default_readiness_budget_accepts_listener_at_25_seconds(monkeypatch):
    """A cold local startup must not be mislabeled as a failed image."""
    elapsed = [0.0]
    real_monotonic = time.monotonic

    def request(_base, path):
        assert path == "/health"
        if elapsed[0] < 25.0:
            raise urllib.error.URLError(ConnectionRefusedError())
        return 200, {}, b'{"version":"1.11.1"}'

    monkeypatch.setattr(smoke, "request", request)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=lambda: elapsed[0],
        sleep=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
    ))
    assert time.monotonic is real_monotonic

    assert smoke.wait_for_health("http://127.0.0.1:8787")[0] == 200
    assert 25.0 <= elapsed[0] <= 30.0
