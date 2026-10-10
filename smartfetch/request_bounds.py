"""Own request envelopes before emergency dispatch."""

import asyncio
from dataclasses import dataclass
import json
import secrets

from .config import (
    MAX_CONSECUTIVE_EMPTY_BODY_CHUNKS,
    MAX_HEADER_AGGREGATE_BYTES,
    MAX_HEADER_COUNT,
    MAX_HEADER_NAME_BYTES,
    MAX_HEADER_VALUE_BYTES,
    MAX_REQUEST_BODY_BYTES,
)


class RequestBoundsError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BoundedRequest:
    body: bytes
    headers: tuple[tuple[bytes, bytes], ...]


def validate_asgi_headers(scope) -> tuple[tuple[bytes, bytes], ...]:
    raw = scope.get("headers")
    if type(raw) is not list or len(raw) > MAX_HEADER_COUNT:
        raise RequestBoundsError("invalid_request")
    owned = []
    total = 0
    lengths = []
    for pair in raw:
        if type(pair) is not tuple or len(pair) != 2:
            raise RequestBoundsError("invalid_request")
        name, value = pair
        if type(name) is not bytes or type(value) is not bytes:
            raise RequestBoundsError("invalid_request")
        if not name or len(name) > MAX_HEADER_NAME_BYTES or len(value) > MAX_HEADER_VALUE_BYTES:
            raise RequestBoundsError("invalid_request")
        total += len(name) + len(value)
        if total > MAX_HEADER_AGGREGATE_BYTES:
            raise RequestBoundsError("invalid_request")
        if name.lower() == b"content-length":
            lengths.append(value)
        owned.append((bytes(name), bytes(value)))
    if len(lengths) > 1:
        raise RequestBoundsError("invalid_request")
    if lengths:
        length = lengths[0]
        if not length or len(length) > 5 or not length.isascii() or not length.isdigit():
            raise RequestBoundsError("invalid_request")
        if int(length) > MAX_REQUEST_BODY_BYTES:
            raise RequestBoundsError("invalid_body_size")
    return tuple(owned)


async def read_bounded_asgi_body(receive) -> bytes:
    body = bytearray()
    empty_chunks = 0
    while True:
        try:
            message = await receive()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RequestBoundsError("invalid_request") from None
        if type(message) is not dict or message.get("type") != "http.request":
            raise RequestBoundsError("invalid_request")
        chunk = message.get("body", b"")
        more_body = message.get("more_body", False)
        if type(chunk) is not bytes or type(more_body) is not bool:
            raise RequestBoundsError("invalid_request")
        if not chunk and more_body:
            empty_chunks += 1
            if empty_chunks > MAX_CONSECUTIVE_EMPTY_BODY_CHUNKS:
                raise RequestBoundsError("invalid_request")
        elif chunk:
            empty_chunks = 0
            remaining = MAX_REQUEST_BODY_BYTES + 1 - len(body)
            body.extend(chunk[:remaining])
            if len(body) > MAX_REQUEST_BODY_BYTES or len(chunk) > remaining:
                raise RequestBoundsError("invalid_body_size")
        if not more_body:
            return bytes(body)


async def _send_rejection(send, code: str) -> None:
    error = "Invalid request body size" if code == "invalid_body_size" else "Invalid request"
    body = json.dumps(
        {"success": False, "error_code": code, "error": error},
        separators=(",", ":"),
    ).encode("ascii")
    await send({
        "type": "http.response.start",
        "status": 400,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class BoundedRequestMiddleware:
    def __init__(self, app, paths=("/fetch", "/mcp")):
        self.app = app
        self.paths = frozenset(paths)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            request_id = secrets.token_hex(8)
            scope["smartfetch.request_id"] = request_id
            original_send = send

            async def send_with_headers(message):
                if message["type"] == "http.response.start":
                    message = dict(message)
                    protected = {b"cache-control", b"x-content-type-options", b"x-request-id"}
                    headers = [
                        (name, value) for name, value in message.get("headers", [])
                        if name.lower() not in protected
                    ]
                    headers.extend([
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-request-id", request_id.encode("ascii")),
                    ])
                    message["headers"] = headers
                await original_send(message)

            send = send_with_headers
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in self.paths
        ):
            await self.app(scope, receive, send)
            return

        try:
            headers = validate_asgi_headers(scope)
            owned = await read_bounded_asgi_body(receive)
        except RequestBoundsError as exc:
            await _send_rejection(send, exc.code)
            return
        scope["smartfetch.bounded_request"] = BoundedRequest(
            body=owned,
            headers=headers,
        )
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": owned, "more_body": False}

        await self.app(scope, replay, send)
