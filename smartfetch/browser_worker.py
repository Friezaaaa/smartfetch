"""Isolated Linux browser worker execution entrypoint.

Runs with network_mode: none and connects only to the local Unix IPC socket.
Validates boundary preconditions (UID 10001, lo only, clean environment)
before launching the Chromium headless renderer.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import select
import signal
import socket
import struct
import sys
import threading
import time
from typing import Any

from smartfetch.browser_pool import WarmChromiumInstance
from smartfetch.browser_prototype.relay import DynamicWorkerRelay
from smartfetch.browser_prototype.worker import (
    WorkerBoundaryError,
    inspect_boundary,
    render_in_isolated_worker,
)

_LOGGER = logging.getLogger("smartfetch.browser_worker")


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


def process_control_job(
    req: dict[str, Any],
    relay: Any,
    instance: Any,
) -> dict[str, Any]:
    """Process a single render or screenshot request for the warm worker server."""
    action = req.get("action", "render")
    url = req["url"]
    socket_path = req["socket_path"]
    raw_op_id = req["operation_id"]
    if isinstance(raw_op_id, str):
        op_id = bytes.fromhex(raw_op_id) if len(raw_op_id) == 32 else raw_op_id.encode("utf-8")
    else:
        op_id = bytes(raw_op_id)
    deadline_sec = float(req.get("deadline_seconds", 20.0))
    deadline = time.monotonic() + deadline_sec

    try:
        relay.set_active_job(socket_path, op_id, deadline)
        if action == "screenshot":
            viewport = str(req.get("viewport", "desktop"))
            full_page = bool(req.get("full_page", False))
            image_format = str(req.get("image_format", "jpeg"))
            quality = int(req.get("quality", 82))
            res = instance.screenshot(
                url,
                deadline=deadline,
                viewport=viewport,
                full_page=full_page,
                image_format=image_format,
                quality=quality,
            )
        else:
            max_chars = int(req.get("max_chars", 20000))
            res = instance.render(
                url,
                deadline=deadline,
                max_chars=max_chars,
                wait_ready=True,
            )
        return {
            "ok": True,
            "result": res,
            "recycle_recommended": not instance.is_healthy(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "screenshot_failed" if action == "screenshot" else "render_failed",
            "error": str(exc),
            "recycle_recommended": True,
        }
    finally:
        relay.clear_active_job()


def serve_warm_worker(
    control_socket: str,
    *,
    chromium: str = "/usr/bin/chromium",
    max_jobs: int = 50,
    max_age_seconds: float = 1800.0,
    boundary_inspector: Any = inspect_boundary,
    instance_factory: Any = None,
    relay_factory: Any = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Run persistent warm headless Chromium worker serving requests over Unix control socket."""
    # 1. Fail closed if boundary violated
    if boundary_inspector is not None:
        boundary_inspector()

    # 2. Launch DynamicWorkerRelay and WarmChromiumInstance
    if relay_factory is not None:
        relay = relay_factory()
    else:
        relay = DynamicWorkerRelay(deadline=float("inf"))

    instance = None
    listener = None
    ctrl_path = Path(control_socket)

    if stop_event is None:
        stop_event = threading.Event()
        def _handle_signal(signum, frame):
            stop_event.set()
        try:
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)
        except (ValueError, AttributeError):
            pass

    try:
        relay_address = relay.start()
        proxy_host, proxy_port = relay_address[0], relay_address[1]

        if instance_factory is not None:
            instance = instance_factory(proxy_host, proxy_port)
        else:
            instance = WarmChromiumInstance(
                instance_id=f"warm-{time.time_ns()}",
                chromium_path=chromium,
                proxy_host=proxy_host,
                proxy_port=proxy_port,
                max_jobs=max_jobs,
                max_age=max_age_seconds,
            )
            instance.start()

        # 3. Listen on control socket with 0o600 permissions
        if ctrl_path.exists():
            try:
                ctrl_path.unlink()
            except OSError:
                pass
        ctrl_path.parent.mkdir(parents=True, exist_ok=True)

        af_unix = getattr(socket, "AF_UNIX", None)
        if af_unix is None:
            raise RuntimeError("AF_UNIX not supported on this platform")

        listener = socket.socket(af_unix, socket.SOCK_STREAM)
        listener.bind(str(ctrl_path))
        try:
            os.chmod(str(ctrl_path), 0o600)
        except OSError:
            pass
        listener.listen(1)
        listener.setblocking(False)

        # 4. Process requests
        while not stop_event.is_set() and instance.is_healthy():
            try:
                readable, _, _ = select.select([listener], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not readable:
                continue

            try:
                conn, _ = listener.accept()
            except (BlockingIOError, OSError):
                continue

            # Verify peer UID 10001 when SO_PEERCRED is available
            if hasattr(socket, "SO_PEERCRED") and hasattr(socket, "SOL_SOCKET"):
                try:
                    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _, peer_uid, _ = struct.unpack("3i", creds)
                    if peer_uid != 10001:
                        conn.close()
                        continue
                except Exception:
                    conn.close()
                    continue

            try:
                conn.settimeout(30.0)
                buf = bytearray()
                while not stop_event.is_set():
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\n" in buf:
                        break
                if not buf:
                    continue
                line = buf.split(b"\n", 1)[0].decode("utf-8").strip()
                if not line:
                    continue
                req = json.loads(line)
                response = process_control_job(req, relay, instance)
                resp_bytes = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
                conn.sendall(resp_bytes)
            except Exception as exc:
                err_resp = {
                    "ok": False,
                    "error_code": "render_failed",
                    "error": str(exc),
                    "recycle_recommended": True,
                }
                try:
                    conn.sendall(json.dumps(err_resp).encode("utf-8") + b"\n")
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

            if not instance.is_healthy():
                break

    finally:
        # 5. Always close Chromium, relay, and control socket
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
        if ctrl_path.exists():
            try:
                ctrl_path.unlink()
            except OSError:
                pass
        if instance is not None:
            try:
                instance.close()
            except Exception:
                pass
        try:
            relay.close()
        except Exception:
            pass

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmartFetch isolated browser worker")
    parser.add_argument("--url", help="Target URL to render")
    parser.add_argument("--socket", help="Path to Unix relay socket")
    parser.add_argument("--operation-id", help="16-byte operation ID in hex")
    parser.add_argument("--deadline-seconds", type=float, default=20.0, help="Per-operation deadline seconds")
    parser.add_argument("--max-chars", type=int, default=20000, help="Max extracted text characters")
    parser.add_argument("--output", default="-", help="Path to write result JSON (or - for stdout)")
    parser.add_argument("--chromium", default="/usr/bin/chromium", help="Path to chromium executable")
    parser.add_argument("--warm-server", action="store_true", help="Run as persistent warm worker server")
    parser.add_argument("--control-socket", help="Path to Unix domain control socket")
    parser.add_argument("--max-jobs", type=int, default=50, help="Max jobs before instance recycling")
    parser.add_argument("--max-age-seconds", type=float, default=1800.0, help="Max lifetime before instance recycling")

    args = parser.parse_args(argv)

    if args.warm_server:
        if not args.control_socket:
            parser.error("--control-socket is required when --warm-server is specified")
        try:
            return serve_warm_worker(
                args.control_socket,
                chromium=args.chromium,
                max_jobs=args.max_jobs,
                max_age_seconds=args.max_age_seconds,
            )
        except WorkerBoundaryError as exc:
            sys.stderr.write(f"Worker boundary violation: {exc.code}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"Warm worker server error: {exc}\n")
            return 1

    if not args.url or not args.socket or not args.operation_id:
        parser.error("--url, --socket, and --operation-id are required when --warm-server is not specified")

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
