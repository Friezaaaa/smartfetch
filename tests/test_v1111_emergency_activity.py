"""Emergency liveness and privacy-safe unavailable-attempt activity."""

import io
import json
import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from smartfetch import activity, server


UNHEALTHY = b'{"success":false,"error_code":"service_unhealthy","error":"Service temporarily unavailable"}'
ALLOWED_KEYS = {"timestamp", "event", "request_id", "surface", "tool", "error_code", "status"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _activity_for(app, request):
    stream = io.StringIO()
    with patch.object(activity._HANDLER, "stream", stream):
        with TestClient(app) as client:
            response = request(client)
    return response, stream.getvalue()


def test_health_discloses_emergency_degraded_capability():
    with TestClient(server.create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "SmartFetch",
        "version": "1.11.1",
        "retrieval_available": False,
        "browser_available": False,
        "mode": "emergency_fail_closed",
    }


def test_terminal_unhealthy_health_is_exact_finite_503():
    app = server.create_app()
    app.app.app.state.terminal_unhealthy = True
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.content == UNHEALTHY
    assert len(response.content) == 92
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.headers["content-length"] == "92"
    assert response.headers["retry-after"] == "3600"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert re.fullmatch(r"[0-9a-f]{16}", response.headers["x-request-id"])


@pytest.mark.parametrize(("surface", "browser"), [
    ("fetch", False), ("fetch", True),
    ("mcp", False), ("mcp", True),
])
def test_one_bounded_allowlisted_unavailable_event_no_input_or_payment(surface, browser):
    app = server.create_app()

    def request(client):
        if surface == "fetch":
            return client.post(
                "/fetch",
                json={"url": "https://example.com/url-canary", "force_browser": browser},
                headers={"PAYMENT-SIGNATURE": "proof-canary", "X-Provider-Canary": "provider-canary"},
            )
        return client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "client-id-canary", "method": "tools/call",
                  "params": {"name": "render_webpage" if browser else "fetch_webpage",
                             "arguments": {"url": "https://example.com/url-canary"}}},
            headers={**MCP_HEADERS, "PAYMENT-SIGNATURE": "proof-canary", "X-Provider-Canary": "provider-canary"},
        )

    response, output = _activity_for(app, request)
    assert response.status_code == (503 if surface == "fetch" else 200)
    lines = output.splitlines()
    assert len(lines) == 1
    assert len((lines[0] + "\n").encode("utf-8")) <= 512
    event = json.loads(lines[0])
    assert set(event) <= ALLOWED_KEYS
    assert event["event"] == "unavailable_attempt"
    assert event["surface"] == surface
    assert event["error_code"] == ("browser_unavailable" if browser else "retrieval_unavailable")
    assert event["status"] == response.status_code
    assert re.fullmatch(r"[0-9a-f]{16}", event["request_id"])
    assert response.headers["x-request-id"] == event["request_id"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", event["timestamp"])
    combined = output + response.text
    for canary in ("url-canary", "proof-canary", "provider-canary"):
        assert canary not in combined
    assert "client-id-canary" not in output
    if surface == "mcp":
        assert response.json()["id"] == "client-id-canary"
    for forbidden in ("payment_challenged", "payment_verified", "payment_settled",
                      "tool_started", "retrieval_started", "tool_completed", "provider_started"):
        assert forbidden not in output


def test_invalid_or_oversized_activity_fields_are_silently_dropped():
    stream = io.StringIO()
    with patch.object(activity._HANDLER, "stream", stream):
        activity.emit_unavailable_attempt(
            request_id="client-chosen-not-random", surface="fetch", tool=None,
            code="retrieval_unavailable", status=503,
        )
    assert stream.getvalue() == ""


def test_serialized_event_over_512_bytes_is_dropped_before_logging():
    stream = io.StringIO()
    with (
        patch.object(activity._HANDLER, "stream", stream),
        patch.object(activity.json, "dumps", return_value="x" * 512),
    ):
        activity.emit_unavailable_attempt(
            request_id="0123456789abcdef", surface="fetch", tool=None,
            code="retrieval_unavailable", status=503,
        )
    assert stream.getvalue() == ""


def test_logger_failure_cannot_change_client_response_or_emit_fallback_line():
    class FailingStream:
        calls = 0

        def write(self, data):
            self.calls += 1
            raise OSError("private-exception-canary")

        def flush(self):
            pass

    stream = FailingStream()
    with patch.object(activity._HANDLER, "stream", stream):
        with TestClient(server.create_app()) as client:
            response = client.post("/fetch", json={"url": "https://example.com/"})
    assert response.status_code == 503
    assert response.json()["error_code"] == "retrieval_unavailable"
    assert "private-exception-canary" not in response.text
    assert stream.calls == 1
