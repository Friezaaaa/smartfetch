"""Privacy-safe structured activity events for SmartFetch operators."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from typing import Iterator, Optional


_LOGGER = logging.getLogger("smartfetch.activity")
_REQUEST_ID: ContextVar[Optional[str]] = ContextVar(
    "smartfetch_activity_request_id",
    default=None,
)
_STRING_FIELDS = frozenset((
    "transport",
    "tool",
    "stage",
    "outcome",
))


def _bounded_string(value, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:limit]


@contextmanager
def activity_context(request_id: str) -> Iterator[None]:
    """Bind a bounded opaque request ID for events in the current task."""
    normalized = _bounded_string(request_id, 128)
    token = _REQUEST_ID.set(normalized)
    try:
        yield
    finally:
        _REQUEST_ID.reset(token)


def emit_activity(event: str, **fields) -> None:
    """Emit one allowlisted JSON event without affecting request behavior."""
    try:
        event_name = _bounded_string(event, 80) or "unknown"
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "event": event_name,
        }
        request_id = _REQUEST_ID.get()
        if request_id is not None:
            record["request_id"] = request_id

        for name in _STRING_FIELDS:
            value = _bounded_string(fields.get(name), 128)
            if value is not None:
                record[name] = value

        status = fields.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            record["status"] = status
        else:
            normalized_status = _bounded_string(status, 80)
            if normalized_status is not None:
                record["status"] = normalized_status

        duration = fields.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            record["duration_ms"] = max(0, int(duration))

        _LOGGER.info(json.dumps(
            record,
            separators=(",", ":"),
            sort_keys=True,
        ))
    except Exception:
        return None
