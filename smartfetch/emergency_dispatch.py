"""Outer fail-closed ASGI dispatch, before payment or retrieval machinery."""

import json
import secrets

from .activity import emit_unavailable_attempt
from .emergency_request import EmergencyRequestError, FreeMCPMethod, classify_fetch, classify_mcp
from .request_bounds import BoundedRequest


_UNAVAILABLE = {
    "retrieval_unavailable": b'{"success":false,"error_code":"retrieval_unavailable","error":"Retrieval is temporarily unavailable"}',
    "browser_unavailable": b'{"success":false,"error_code":"browser_unavailable","error":"Browser rendering is temporarily unavailable"}',
    "service_unhealthy": b'{"success":false,"error_code":"service_unhealthy","error":"Service temporarily unavailable"}',
}
_ERRORS = {
    "invalid_request": b'{"success":false,"error_code":"invalid_request","error":"Invalid request"}',
    "invalid_json": b'{"success":false,"error_code":"invalid_json","error":"Invalid JSON"}',
    "unsupported_media_type": b'{"success":false,"error_code":"unsupported_media_type","error":"Unsupported media type"}',
}
_METHOD_NOT_ALLOWED = b'{"success":false,"error_code":"method_not_allowed","error":"Method not allowed"}'


def _compact(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _mcp_unavailable(jsonrpc_id, browser: bool) -> bytes:
    code = "browser_unavailable" if browser else "retrieval_unavailable"
    message = (
        "Browser rendering is temporarily unavailable"
        if browser else "Retrieval is temporarily unavailable"
    )
    return _compact({
        "jsonrpc": "2.0",
        "id": jsonrpc_id,
        "result": {
            "content": [{"type": "text", "text": message}],
            "structuredContent": {"success": False, "error_code": code, "error": message},
            "isError": True,
        },
    })


def _mcp_error(jsonrpc_id, code: int, message: str) -> bytes:
    return _compact({"jsonrpc": "2.0", "id": jsonrpc_id, "error": {"code": code, "message": message}})


async def _send_json(send, status: int, body: bytes, *, unavailable: bool = False, allow: bool = False):
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if unavailable:
        headers.append((b"retry-after", b"3600"))
    if allow:
        headers.append((b"allow", b"POST"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class EmergencyDispatch:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") == "http"
            and scope.get("path") != "/health"
            and getattr(self.app.state, "terminal_unhealthy", False)
        ):
            await _send_json(send, 503, _UNAVAILABLE["service_unhealthy"], unavailable=True)
            return
        if scope.get("type") != "http" or scope.get("path") not in ("/fetch", "/mcp"):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        method = scope.get("method")
        if method != "POST":
            body = b"" if method == "HEAD" else _METHOD_NOT_ALLOWED
            await _send_json(send, 405, body, allow=True)
            return
        bounded = scope.get("smartfetch.bounded_request")
        if type(bounded) is not BoundedRequest:
            await _send_json(send, 400, _ERRORS["invalid_request"])
            return
        if path == "/mcp":
            try:
                call = classify_mcp(bounded.body, bounded.headers)
            except EmergencyRequestError as exc:
                if exc.code == "method_not_available":
                    body = _mcp_error(exc.jsonrpc_id, -32601, "Method not available")
                elif exc.code == "unknown_tool":
                    body = _mcp_error(exc.jsonrpc_id, -32602, "Unknown tool")
                else:
                    body = _ERRORS[exc.code]
                await _send_json(send, exc.status, body)
                return
            if isinstance(call, FreeMCPMethod):
                await self.app(scope, receive, send)
                return
            await _send_json(send, 200, _mcp_unavailable(call.jsonrpc_id, call.browser))
            try:
                emit_unavailable_attempt(
                    request_id=secrets.token_hex(8),
                    surface="mcp",
                    tool=call.tool,
                    code="browser_unavailable" if call.browser else "retrieval_unavailable",
                    status=200,
                )
            except Exception:
                pass
            return
        try:
            call = classify_fetch(bounded.body, bounded.headers)
        except EmergencyRequestError as exc:
            await _send_json(send, exc.status, _ERRORS[exc.code])
            return
        code = "browser_unavailable" if call.browser else "retrieval_unavailable"
        await _send_json(send, 503, _UNAVAILABLE[code], unavailable=True)
        try:
            emit_unavailable_attempt(
                request_id=secrets.token_hex(8),
                surface="fetch",
                tool=None,
                code=code,
                status=503,
            )
        except Exception:
            pass
