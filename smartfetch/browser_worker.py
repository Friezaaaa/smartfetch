"""Isolated Linux browser worker execution entrypoint.

Runs with network_mode: none and connects only to the local Unix IPC socket.
Validates boundary preconditions (UID 10001, lo only, clean environment)
before launching the Chromium headless renderer.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from smartfetch.browser_prototype.worker import (
    WorkerBoundaryError,
    inspect_boundary,
    render_in_isolated_worker,
)


def run_worker_job(
    url: str,
    *,
    socket_path: str,
    operation_id: bytes,
    chromium: str = "/usr/bin/chromium",
    deadline: float,
    max_chars: int = 20000,
) -> dict[str, Any]:
    """Execute isolated rendering through the verified Unix IPC relay."""
    return render_in_isolated_worker(
        url,
        socket_path=socket_path,
        operation_id=operation_id,
        chromium=chromium,
        deadline=deadline,
        max_chars=max_chars,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmartFetch isolated browser worker")
    parser.add_argument("--url", required=True, help="Target URL to render")
    parser.add_argument("--socket", required=True, help="Path to Unix relay socket")
    parser.add_argument("--operation-id", required=True, help="16-byte operation ID in hex")
    parser.add_argument("--deadline-seconds", type=float, default=20.0, help="Per-operation deadline seconds")
    parser.add_argument("--max-chars", type=int, default=20000, help="Max extracted text characters")
    parser.add_argument("--output", default="-", help="Path to write result JSON (or - for stdout)")
    parser.add_argument("--chromium", default="/usr/bin/chromium", help="Path to chromium executable")

    args = parser.parse_args(argv)

    try:
        op_id_bytes = bytes.fromhex(args.operation_id)
        if len(op_id_bytes) != 16:
            raise ValueError("Operation ID must be exactly 16 bytes (32 hex characters)")
    except ValueError as exc:
        err_envelope = {"success": False, "error_code": "invalid_request", "error": str(exc)}
        _write_output(args.output, err_envelope)
        return 1

    deadline = time.monotonic() + args.deadline_seconds
    try:
        result = run_worker_job(
            args.url,
            socket_path=args.socket,
            operation_id=op_id_bytes,
            chromium=args.chromium,
            deadline=deadline,
            max_chars=args.max_chars,
        )
        _write_output(args.output, result)
        return 0
    except WorkerBoundaryError as exc:
        err_envelope = {
            "success": False,
            "error_code": "render_failed",
            "phase": "browser_start",
            "error": f"Worker boundary violation: {exc.code}",
        }
        _write_output(args.output, err_envelope)
        return 1
    except Exception as exc:
        err_envelope = {
            "success": False,
            "error_code": "render_failed",
            "phase": "browser_navigate",
            "error": str(exc),
        }
        _write_output(args.output, err_envelope)
        return 1


def _write_output(output_target: str, data: dict[str, Any]) -> None:
    encoded = json.dumps(data, ensure_ascii=False)
    if output_target == "-":
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()
    else:
        Path(output_target).write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
