"""The emergency runtime cannot be configured back into executable retrieval."""

from pathlib import Path
import os
import socket
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from smartfetch import mcp_server, server


ROOT = Path(__file__).resolve().parents[1]


class ForbiddenInput:
    def __getattribute__(self, name):
        raise AssertionError("activation or payment input was read")


@pytest.mark.parametrize("settings", [
    {},
    {"SMARTFETCH_V111_ENABLED": "true", "EXA_API_KEY": "private-canary"},
    {"SMARTFETCH_V111_ENABLED": "false", "GEMINI_API_KEY": "private-canary"},
    {"SMARTFETCH_V111_ENABLED": "malformed", "X402_PAYEE_ADDRESS": "private-canary"},
])
def test_activation_inputs_cannot_create_an_executable_app(settings):
    with (
        patch.object(socket, "create_connection", side_effect=AssertionError("socket opened")) as network,
        patch.object(subprocess, "Popen", side_effect=AssertionError("worker started")) as worker,
    ):
        app = server.create_app(
            payment_settings=ForbiddenInput(),
            v111_runtime=ForbiddenInput(),
            v111_environ=settings,
        )
        with TestClient(app) as client:
            health = client.get("/health")
            fetch = client.post("/fetch", json={"url": "https://example.com/"})
            mcp = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "fetch_webpage", "arguments": {"url": "https://example.com/"}},
            }, headers={"Accept": "application/json, text/event-stream"})
        assert health.json()["mode"] == "emergency_fail_closed"
        assert fetch.status_code == 503
        assert mcp.json()["result"]["isError"] is True
        network.assert_not_called()
        worker.assert_not_called()


def test_direct_legacy_mcp_factory_is_still_emergency_only():
    mcp = mcp_server.create_smartfetch_mcp(ForbiddenInput(), ForbiddenInput(), v111_service=ForbiddenInput())
    assert mcp.resource_server is None
    assert mcp.accepts == []


def test_dockerfile_has_no_browser_media_or_root_runtime():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").lower()
    assert "user 10001:10001" in dockerfile
    assert "chromium" not in dockerfile
    assert "ffmpeg" not in dockerfile
    assert "ffprobe" not in dockerfile
    assert "max_concurrent_browsers" not in dockerfile
    assert 'cmd ["python", "-m", "smartfetch.server"]' in dockerfile
    assert dockerfile.count("cmd [") == 1
    assert "entrypoint" not in dockerfile


def test_server_entrypoint_does_not_read_provider_or_payment_secrets():
    source = (ROOT / "smartfetch" / "server.py").read_text(encoding="utf-8")
    for secret_name in ("EXA_API_KEY", "GEMINI_API_KEY", "PAYMENT_PRIVATE_KEY", "X402_FACILITATOR_TOKEN"):
        assert secret_name not in source
    assert "retrieval_available" in source
    assert "browser_available" in source
