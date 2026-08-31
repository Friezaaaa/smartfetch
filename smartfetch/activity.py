"""Privacy-safe structured activity events for SmartFetch operators."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import sys
from typing import Iterator, Optional

from .diagnostics import (
    SAFE_FAILURE_CODES,
    SAFE_PHASES,
    SAFE_STRATEGIES,
    safe_diagnostic_host,
)


_LOGGER = logging.getLogger("smartfetch.activity")
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False
_LOGGER.handlers.clear()
_HANDLER = logging.StreamHandler(sys.stdout)
_HANDLER.setLevel(logging.INFO)
_HANDLER.setFormatter(logging.Formatter("%(message)s"))
_HANDLER._smartfetch_activity_handler = True
_LOGGER.addHandler(_HANDLER)
_REQUEST_ID: ContextVar[Optional[str]] = ContextVar(
    "smartfetch_activity_request_id",
    default=None,
)
_REQUEST_FIELDS: ContextVar[dict] = ContextVar(
    "smartfetch_activity_request_fields",
    default={},
)
_STRING_FIELDS = frozenset((
    "transport",
    "tool",
    "route",
    "stage",
    "outcome",
    "payment_stage",
    "payment_network",
    "payment_asset",
    "payment_amount",
    "failure_reason",
    "client_category",
))
_SAFE_FAILURE_REASONS = frozenset((
    "invalid_payment",
    "payment_rejected",
    "payment_required",
    "retrieval_failed",
    "settlement_failed",
    "target_rejected",
    "timeout",
    "verification_failed",
))
_DIAGNOSTIC_BOOLEAN_FIELDS = frozenset((
    'http_attempted',
    'http_retry_attempted',
    'browser_attempted',
    'fallback_attempted',
))


def _bounded_string(value, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:limit]


@contextmanager
def activity_context(request_id: str, **fields) -> Iterator[None]:
    """Bind a bounded opaque request ID for events in the current task."""
    normalized = _bounded_string(request_id, 128)
    request_token = _REQUEST_ID.set(normalized)
    fields_token = _REQUEST_FIELDS.set(dict(fields))
    try:
        yield
    finally:
        _REQUEST_FIELDS.reset(fields_token)
        _REQUEST_ID.reset(request_token)


def _is_error_event(event_name: str, outcome: Optional[str]) -> bool:
    return outcome in {"failed", "timeout"} or (
        event_name == "payment_settled" and outcome != "settled"
    )


def emit_activity(event: str, **fields) -> None:
    """Emit one allowlisted JSON event without affecting request behavior."""
    try:
        event_name = _bounded_string(event, 80) or "unknown"
        effective_fields = dict(_REQUEST_FIELDS.get())
        effective_fields.update(fields)
        outcome = _bounded_string(effective_fields.get("outcome"), 128)
        is_error = _is_error_event(event_name, outcome)
        level = "ERROR" if is_error else "INFO"
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "event": event_name,
            "message": event_name,
            "level": level,
        }
        request_id = _REQUEST_ID.get()
        if request_id is not None:
            record["request_id"] = request_id

        for name in _STRING_FIELDS:
            value = _bounded_string(effective_fields.get(name), 128)
            if name == "failure_reason" and value not in _SAFE_FAILURE_REASONS:
                value = None
            if value is not None:
                record[name] = value

        payment_present = effective_fields.get("payment_present")
        if isinstance(payment_present, bool):
            record["payment_present"] = payment_present

        status = effective_fields.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            record["status"] = status
        else:
            normalized_status = _bounded_string(status, 80)
            if normalized_status is not None:
                record["status"] = normalized_status

        duration = effective_fields.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            record["duration_ms"] = max(0, int(duration))

        if event_name == 'tool_failed':
            target_host = safe_diagnostic_host(
                effective_fields.get('target_host')
            )
            if target_host is not None:
                record['target_host'] = target_host

            strategy = effective_fields.get('strategy')
            if isinstance(strategy, str) and strategy in SAFE_STRATEGIES:
                record['strategy'] = strategy

            phase = effective_fields.get('phase')
            if isinstance(phase, str) and phase in SAFE_PHASES:
                record['phase'] = phase

            failure_code = effective_fields.get('failure_code')
            if failure_code is not None:
                record['failure_code'] = (
                    failure_code
                    if isinstance(failure_code, str)
                    and failure_code in SAFE_FAILURE_CODES
                    else 'unknown'
                )

            for name in _DIAGNOSTIC_BOOLEAN_FIELDS:
                value = effective_fields.get(name)
                if isinstance(value, bool):
                    record[name] = value

            upstream_status = effective_fields.get('upstream_status')
            if (
                isinstance(upstream_status, int)
                and not isinstance(upstream_status, bool)
                and 100 <= upstream_status <= 599
            ):
                record['upstream_status'] = upstream_status

        log = _LOGGER.error if is_error else _LOGGER.info
        log(json.dumps(
            record,
            separators=(",", ":"),
            sort_keys=True,
        ))
    except Exception:
        return None
