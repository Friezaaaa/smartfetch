"""Emergency request-envelope tests against the real ASGI boundary."""

import asyncio
import importlib
import json

import pytest


def _send_request(body: bytes, *, headers=(), messages=None):
    bounds = importlib.import_module("smartfetch.request_bounds")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/fetch",
        "headers": list(headers),
    }
    inbound = list(messages or [
        {"type": "http.request", "body": body, "more_body": False},
    ])
    observed = {"consumed": 0, "downstream": 0, "body": None, "sent": []}

    async def receive():
        i = observed["consumed"]
        if i >= len(inbound):
            return {"type": "http.disconnect"}
        observed["consumed"] += 1
        return inbound[i]

    async def send(message):
        observed["sent"].append(message)

    async def downstream(received_scope, replay, reply):
        observed["downstream"] += 1
        observed["bounded"] = received_scope["smartfetch.bounded_request"]
        observed["body"] = await replay()
        await reply({"type": "http.response.start", "status": 204, "headers": []})
        await reply({"type": "http.response.body", "body": b""})

    asyncio.run(bounds.BoundedRequestMiddleware(downstream)(scope, receive, send))
    return observed


def test_exact_20000_byte_body_is_owned_and_replayed_once():
    body = b"x" * 20_000
    result = _send_request(body, headers=[(b"content-length", b"20000")])

    assert result["downstream"] == 1
    assert result["consumed"] == 1
    assert result["bounded"].body == body
    assert result["body"] == {
        "type": "http.request",
        "body": body,
        "more_body": False,
    }
    assert result["sent"][0]["status"] == 204


@pytest.mark.parametrize(
    ("headers", "messages", "consumed", "code"),
    [
        ([(b"content-length", b"20001")], None, 0, "invalid_body_size"),
        ([(b"content-length", b"9" * 5000)], None, 0, "invalid_request"),
        ([], [{"type": "http.request", "body": b"x" * 20_001, "more_body": False}], 1, "invalid_body_size"),
        ([(b"content-length", b"1")], [
            {"type": "http.request", "body": b"x" * 4096, "more_body": True}
            for _ in range(6)
        ], 5, "invalid_body_size"),
        ([], [
            {"type": "http.request", "body": b"", "more_body": True}
            for _ in range(65)
        ], 65, "invalid_request"),
        ([], [{"type": "http.disconnect"}], 1, "invalid_request"),
        ([], [{"type": "http.request", "body": "not bytes", "more_body": False}], 1, "invalid_request"),
    ],
)
def test_bad_envelope_rejects_before_downstream(headers, messages, consumed, code):
    result = _send_request(b"{}", headers=headers, messages=messages)

    assert result["downstream"] == 0
    assert result["consumed"] == consumed
    assert result["sent"][0]["status"] == 400
    payload = json.loads(result["sent"][1]["body"])
    assert payload["error_code"] == code
    assert len(result["sent"][1]["body"]) <= 128


@pytest.mark.parametrize(
    ("headers", "accepted"),
    [
        ([(b"x", b"")] * 64, True),
        ([(b"x", b"")] * 65, False),
        ([(b"x" * 256, b"")], True),
        ([(b"x" * 257, b"")], False),
        ([(b"x", b"v" * 8192)], True),
        ([(b"x", b"v" * 8193)], False),
        ([(b"x", b"v" * 8192), (b"y", b"w" * 8190)], True),
        ([(b"x", b"v" * 8192), (b"y", b"w" * 8191)], False),
        ([(b"Content-Length", b"2"), (b"content-length", b"2")], False),
    ],
)
def test_header_boundaries_are_enforced_before_body_read(headers, accepted):
    result = _send_request(b"{}", headers=headers)

    assert result["downstream"] == int(accepted)
    assert result["consumed"] == int(accepted)
    assert result["sent"][0]["status"] == (204 if accepted else 400)


def test_payment_header_values_are_opaque_to_envelope_validation():
    result = _send_request(
        b"{}",
        headers=[
            (b"PAYMENT-SIGNATURE", b"opaque-proof"),
            (b"payment-signature", b"not-json"),
        ],
    )
    assert result["downstream"] == 1
    assert result["sent"][0]["status"] == 204


JSON_HEADERS = ((b"content-type", b"application/json"),)


def _classify_fetch(payload, headers=JSON_HEADERS):
    emergency = importlib.import_module("smartfetch.emergency_request")
    return emergency.classify_fetch(payload, headers)


def _classify_mcp(payload, headers=JSON_HEADERS):
    emergency = importlib.import_module("smartfetch.emergency_request")
    return emergency.classify_mcp(payload, headers)


def test_valid_fetch_classification_distinguishes_forced_browser():
    ordinary = _classify_fetch(b'{"url":"https://example.com/","max_chars":1000}')
    browser = _classify_fetch(b'{"url":"https://example.com/","force_browser":true}')
    assert (ordinary.transport, ordinary.tool, ordinary.browser) == (
        "http", "fetch_webpage", False,
    )
    assert (browser.transport, browser.tool, browser.browser) == (
        "http", "fetch_webpage", True,
    )


@pytest.mark.parametrize(
    ("payload", "headers", "status"),
    [
        (b'{"url":"https://example.com/"}', ((b"content-type", b"text/plain"),), 415),
        (b"\xff", JSON_HEADERS, 400),
        (b"{", JSON_HEADERS, 400),
        (b"[]", JSON_HEADERS, 400),
        (b'{"max_chars":1000}', JSON_HEADERS, 422),
        (b'{"url":"https://example.com/","max_chars":999}', JSON_HEADERS, 422),
        (b'{"url":"https://example.com/","max_chars":50001}', JSON_HEADERS, 422),
        (b'{"url":"https://example.com/","force_browser":"yes"}', JSON_HEADERS, 422),
    ],
)
def test_bad_fetch_input_fails_before_unavailability(payload, headers, status):
    emergency = importlib.import_module("smartfetch.emergency_request")
    with pytest.raises(emergency.EmergencyRequestError) as caught:
        emergency.classify_fetch(payload, headers)
    assert caught.value.status == status
    assert caught.value.code in {"invalid_request", "invalid_json", "unsupported_media_type"}


def _mcp_message(method, *, request_id=1, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def test_mcp_only_four_lifecycle_methods_are_free():
    initialized = _classify_mcp(_mcp_message("initialize", params={
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "emergency-test", "version": "1"},
    }))
    assert (initialized.method, initialized.jsonrpc_id) == ("initialize", 1)
    for method in ("ping", "tools/list"):
        classified = _classify_mcp(_mcp_message(method))
        assert (classified.method, classified.jsonrpc_id) == (method, 1)
    notification = _classify_mcp(b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
    assert notification.method == "notifications/initialized"
    assert notification.jsonrpc_id is None


def test_mcp_known_tools_are_classified_without_payment_parsing():
    headers = JSON_HEADERS + (
        (b"PAYMENT-SIGNATURE", b"opaque-canary"),
        (b"payment-signature", b"{malformed-proof"),
    )
    for name in ("fetch_webpage", "webpage_to_markdown", "extract_webpage_text"):
        params = {"name": name, "arguments": {"url": "https://example.com/"}}
        ordinary = _classify_mcp(_mcp_message("tools/call", params=params), headers)
        assert (ordinary.transport, ordinary.tool, ordinary.browser, ordinary.jsonrpc_id) == (
            "mcp", name, False, 1,
        )
        params["arguments"]["force_browser"] = True
        browser = _classify_mcp(_mcp_message("tools/call", params=params), headers)
        assert browser.browser is True
    rendered = _classify_mcp(_mcp_message("tools/call", params={
        "name": "render_webpage", "arguments": {"url": "https://example.com/"},
    }), headers)
    assert (rendered.tool, rendered.browser) == ("render_webpage", True)


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        (b"[]", 400, "invalid_request"),
        (b"{", 400, "invalid_json"),
        (_mcp_message("tools/list", request_id=True), 400, "invalid_request"),
        (_mcp_message("tools/list", request_id="x" * 127), 400, "invalid_request"),
        (b'{"jsonrpc":"2.0","method":"tools/list"}', 400, "invalid_request"),
        (_mcp_message("resources/list"), 200, "method_not_available"),
        (_mcp_message("tools/call", params={"name":"unknown","arguments":{}}), 200, "unknown_tool"),
        (_mcp_message("tools/call", params={"name":"fetch_webpage","arguments":{"url":123}}), 422, "invalid_request"),
        (b'{"jsonrpc":"2.0","method":"notifications/unknown"}', 400, "invalid_request"),
    ],
)
def test_mcp_invalid_or_disallowed_requests_have_finite_classification(payload, status, code):
    emergency = importlib.import_module("smartfetch.emergency_request")
    with pytest.raises(emergency.EmergencyRequestError) as caught:
        emergency.classify_mcp(payload, JSON_HEADERS)
    assert (caught.value.status, caught.value.code) == (status, code)


def test_jsonrpc_id_exact_128_byte_serialization_is_accepted():
    classified = _classify_mcp(_mcp_message("tools/list", request_id="x" * 126))
    assert classified.jsonrpc_id == "x" * 126
