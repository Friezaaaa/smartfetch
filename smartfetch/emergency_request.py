"""Pure, bounded classification of emergency HTTP and MCP requests."""

from dataclasses import dataclass
import json
from typing import Literal

from jsonschema import Draft202012Validator, FormatChecker
from mcp.types import InitializeRequest, InitializedNotification, ListToolsRequest, PingRequest
from pydantic import ValidationError

from .bazaar import FETCH_INPUT_SCHEMA, RENDER_INPUT_SCHEMA


LEGACY_TOOLS = (
    "fetch_webpage",
    "webpage_to_markdown",
    "extract_webpage_text",
    "render_webpage",
)
FREE_MCP_METHODS = (
    "initialize",
    "notifications/initialized",
    "ping",
    "tools/list",
)
_FETCH_VALIDATOR = Draft202012Validator(FETCH_INPUT_SCHEMA, format_checker=FormatChecker())
_RENDER_VALIDATOR = Draft202012Validator(RENDER_INPUT_SCHEMA, format_checker=FormatChecker())
MCP_FETCH_ARGUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
        "force_browser": {"type": "boolean"},
    },
    "required": ["url"],
}
MCP_RENDER_ARGUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
    },
    "required": ["url"],
}
_MCP_FETCH_VALIDATOR = Draft202012Validator(MCP_FETCH_ARGUMENT_SCHEMA)
_MCP_RENDER_VALIDATOR = Draft202012Validator(MCP_RENDER_ARGUMENT_SCHEMA)
_FREE_MCP_MODELS = {
    "initialize": InitializeRequest,
    "notifications/initialized": InitializedNotification,
    "ping": PingRequest,
    "tools/list": ListToolsRequest,
}


class EmergencyRequestError(ValueError):
    def __init__(self, status: int, code: str, jsonrpc_id: str | int | None = None):
        self.status = status
        self.code = code
        self.jsonrpc_id = jsonrpc_id
        super().__init__(code)


@dataclass(frozen=True)
class ValidatedCall:
    transport: Literal["http", "mcp"]
    tool: str
    browser: bool
    jsonrpc_id: str | int | None


@dataclass(frozen=True)
class FreeMCPMethod:
    method: Literal["initialize", "notifications/initialized", "ping", "tools/list"]
    jsonrpc_id: str | int | None


def _parse_json(body: bytes, headers: tuple[tuple[bytes, bytes], ...]) -> dict:
    content_types = [value for name, value in headers if name.lower() == b"content-type"]
    if len(content_types) != 1:
        raise EmergencyRequestError(415, "unsupported_media_type")
    media_type = content_types[0].split(b";", 1)[0].strip().lower()
    if media_type != b"application/json":
        raise EmergencyRequestError(415, "unsupported_media_type")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise EmergencyRequestError(400, "invalid_json") from None
    if type(parsed) is not dict:
        raise EmergencyRequestError(400, "invalid_request")
    return parsed


def _validate_arguments(arguments: dict, *, render: bool = False, mcp: bool = False) -> None:
    validator = (
        (_MCP_RENDER_VALIDATOR if render else _MCP_FETCH_VALIDATOR)
        if mcp else (_RENDER_VALIDATOR if render else _FETCH_VALIDATOR)
    )
    if not validator.is_valid(arguments):
        raise EmergencyRequestError(422, "invalid_request")


def _validate_free_mcp_params(payload: dict, method: str) -> None:
    try:
        _FREE_MCP_MODELS[method].model_validate(payload)
    except ValidationError:
        raise EmergencyRequestError(400, "invalid_request") from None


def classify_fetch(
    body: bytes,
    headers: tuple[tuple[bytes, bytes], ...],
) -> ValidatedCall:
    payload = _parse_json(body, headers)
    _validate_arguments(payload)
    return ValidatedCall(
        transport="http",
        tool="fetch_webpage",
        browser=payload.get("force_browser") is True,
        jsonrpc_id=None,
    )


def classify_mcp(
    body: bytes,
    headers: tuple[tuple[bytes, bytes], ...],
) -> ValidatedCall | FreeMCPMethod:
    payload = _parse_json(body, headers)
    method = payload.get("method")
    if payload.get("jsonrpc") != "2.0" or type(method) is not str or not method:
        raise EmergencyRequestError(400, "invalid_request")
    if method == "notifications/initialized":
        if "id" in payload:
            raise EmergencyRequestError(400, "invalid_request")
        _validate_free_mcp_params(payload, method)
        return FreeMCPMethod(method=method, jsonrpc_id=None)

    request_id = payload.get("id")
    if type(request_id) not in (str, int):
        raise EmergencyRequestError(400, "invalid_request")
    if type(request_id) is str:
        try:
            request_id.encode("utf-8")
        except UnicodeEncodeError:
            raise EmergencyRequestError(400, "invalid_request") from None
    if len(json.dumps(request_id, separators=(",", ":")).encode("utf-8")) > 128:
        raise EmergencyRequestError(400, "invalid_request")
    if method in FREE_MCP_METHODS:
        _validate_free_mcp_params(payload, method)
        return FreeMCPMethod(method=method, jsonrpc_id=request_id)
    if method != "tools/call":
        raise EmergencyRequestError(200, "method_not_available", request_id)

    params = payload.get("params")
    if type(params) is not dict:
        raise EmergencyRequestError(422, "invalid_request", request_id)
    tool = params.get("name")
    if type(tool) is not str or tool not in LEGACY_TOOLS:
        raise EmergencyRequestError(200, "unknown_tool", request_id)
    arguments = params.get("arguments", {})
    if type(arguments) is not dict:
        raise EmergencyRequestError(422, "invalid_request", request_id)
    _validate_arguments(arguments, render=tool == "render_webpage", mcp=True)
    return ValidatedCall(
        transport="mcp",
        tool=tool,
        browser=tool == "render_webpage" or arguments.get("force_browser") is True,
        jsonrpc_id=request_id,
    )
