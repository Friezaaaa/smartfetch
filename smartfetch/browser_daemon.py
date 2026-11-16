"""Production-ready VPS Browser Worker Daemon.

Exposes an authenticated HTTPS/mTLS service matching the smartfetch/remote_browser.py
interface contract. Enforces HMAC-SHA256 authentication, replay prevention,
SSRF destination policy, capacity bounding, and the Phase 1 Unix socket
broker-to-worker isolation architecture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from smartfetch.browser_prototype.broker import Broker
from smartfetch.browser_prototype.policy import DestinationPolicy, PolicyError
from smartfetch.browser_prototype.relay import BridgeServer
from smartfetch.browser_prototype.worker import (
    WorkerBoundaryError,
    render_in_isolated_worker,
)
from smartfetch.remote_browser import (
    ERROR_BROWSER_UNAVAILABLE,
    ERROR_INVALID_REQUEST,
    ERROR_POLICY_REJECTION,
    ERROR_RENDER_FAILED,
    ERROR_TIMEOUT,
    verify_signature,
)
from smartfetch.security import DNSResolutionError, validate_public_url

_LOGGER = logging.getLogger("smartfetch.browser_daemon")
_MAX_REQUEST_BYTES = 65_536


@dataclass
class NonceReplayCache:
    """Thread-safe TTL replay cache for request nonces."""

    ttl_seconds: float = 60.0
    _seen: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check_and_add(self, nonce: str) -> bool:
        """Return True if nonce is fresh, False if already seen (replay)."""
        now = time.monotonic()
        with self._lock:
            # Purge expired nonces
            expired = [k for k, exp in self._seen.items() if now >= exp]
            for k in expired:
                del self._seen[k]

            if nonce in self._seen:
                return False
            self._seen[nonce] = now + self.ttl_seconds
            return True


@dataclass(frozen=True)
class BrowserDaemonConfig:
    """Configuration for the VPS browser worker daemon."""

    host: str = "0.0.0.0"
    port: int = 8443
    hmac_key: str | None = None
    max_concurrent: int = 2
    ipc_dir: str = "/ipc"
    ipc_volume: str | None = None
    chromium_path: str = "/usr/bin/chromium"
    worker_mode: str = "direct"  # direct, subprocess, docker
    docker_image: str = "smartfetch-prototype-worker:local"
    seccomp_profile: str = "/etc/smartfetch/seccomp-chrome.json"
    ssl_cert: str | None = None
    ssl_key: str | None = None
    ssl_ca: str | None = None
    max_request_bytes: int = _MAX_REQUEST_BYTES
    max_response_bytes: int = 10_485_760

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BrowserDaemonConfig:
        environ = os.environ if env is None else env
        return cls(
            host=environ.get("BROWSER_WORKER_HOST", "0.0.0.0").strip(),
            port=int(environ.get("BROWSER_WORKER_PORT", "8443")),
            hmac_key=environ.get("BROWSER_WORKER_HMAC_KEY", "").strip() or None,
            max_concurrent=int(environ.get("BROWSER_WORKER_MAX_CONCURRENT", "2")),
            ipc_dir=environ.get("BROWSER_WORKER_IPC_DIR", "/ipc").strip(),
            ipc_volume=environ.get("BROWSER_WORKER_IPC_VOLUME", "").strip() or None,
            chromium_path=environ.get("BROWSER_WORKER_CHROMIUM_PATH", "/usr/bin/chromium").strip(),
            worker_mode=environ.get("BROWSER_WORKER_MODE", "direct").strip(),
            docker_image=environ.get("BROWSER_WORKER_DOCKER_IMAGE", "smartfetch-prototype-worker:local").strip(),
            seccomp_profile=environ.get("BROWSER_WORKER_SECCOMP", "/etc/smartfetch/seccomp-chrome.json").strip(),
            ssl_cert=environ.get("BROWSER_WORKER_SSL_CERT", "").strip() or None,
            ssl_key=environ.get("BROWSER_WORKER_SSL_KEY", "").strip() or None,
            ssl_ca=environ.get("BROWSER_WORKER_SSL_CA", "").strip() or None,
        )


class WorkerRunner:
    """Interface for dispatching worker execution."""

    def run(
        self,
        url: str,
        *,
        socket_path: str,
        operation_id: bytes,
        deadline: float,
        max_chars: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        raise NotImplementedError


class DirectWorkerRunner(WorkerRunner):
    """Executes the isolated worker in-process."""

    def __init__(self, chromium_path: str = "/usr/bin/chromium"):
        self.chromium_path = chromium_path

    def run(
        self,
        url: str,
        *,
        socket_path: str,
        operation_id: bytes,
        deadline: float,
        max_chars: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return render_in_isolated_worker(
            url,
            socket_path=socket_path,
            operation_id=operation_id,
            chromium=self.chromium_path,
            deadline=deadline,
            max_chars=max_chars,
        )


class SubprocessWorkerRunner(WorkerRunner):
    """Executes the worker via python -m smartfetch.browser_worker."""

    def __init__(self, chromium_path: str = "/usr/bin/chromium"):
        self.chromium_path = chromium_path

    def run(
        self,
        url: str,
        *,
        socket_path: str,
        operation_id: bytes,
        deadline: float,
        max_chars: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        output_file = Path(socket_path).parent / "worker_output.json"
        cmd = [
            sys.executable,
            "-m",
            "smartfetch.browser_worker",
            "--url",
            url,
            "--socket",
            socket_path,
            "--operation-id",
            operation_id.hex(),
            "--deadline-seconds",
            str(timeout_seconds),
            "--max-chars",
            str(max_chars),
            "--output",
            str(output_file),
            "--chromium",
            self.chromium_path,
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds + 3.0,
            text=True,
        )
        if proc.returncode != 0 or not output_file.is_file():
            raise RuntimeError(f"Subprocess worker failed (exit {proc.returncode}): {proc.stderr}")
        return json.loads(output_file.read_text(encoding="utf-8"))


class DockerWorkerRunner(WorkerRunner):
    """Spawns an ephemeral Docker worker container per job."""

    def __init__(
        self,
        docker_image: str = "smartfetch-prototype-worker:local",
        seccomp_profile: str = "/etc/smartfetch/seccomp-chrome.json",
        ipc_volume: str | None = None,
    ):
        self.docker_image = docker_image
        self.seccomp_profile = seccomp_profile
        self.ipc_volume = ipc_volume

    def run(
        self,
        url: str,
        *,
        socket_path: str,
        operation_id: bytes,
        deadline: float,
        max_chars: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        job_dir = Path(socket_path).parent.resolve()
        job_name = job_dir.name
        container_name = f"sf-worker-{secrets.token_hex(6)}"

        if self.ipc_volume:
            volume_mount = f"{self.ipc_volume}:/ipc:rw"
            container_socket = f"/ipc/{job_name}/relay.sock"
            container_output = f"/ipc/{job_name}/worker_output.json"
        else:
            volume_mount = f"{job_dir}:/ipc:rw"
            container_socket = "/ipc/relay.sock"
            container_output = "/ipc/worker_output.json"

        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--user",
            "10001:10001",
            "--memory",
            "768m",
            "--cpus",
            "1.0",
            "--pids-limit",
            "256",
            "-v",
            volume_mount,
        ]
        if os.path.isfile(self.seccomp_profile):
            cmd.append(f"--security-opt=seccomp={self.seccomp_profile}")
        cmd.extend([
            self.docker_image,
            "python",
            "-m",
            "smartfetch.browser_worker",
            "--url",
            url,
            "--socket",
            container_socket,
            "--operation-id",
            operation_id.hex(),
            "--deadline-seconds",
            str(timeout_seconds),
            "--max-chars",
            str(max_chars),
            "--output",
            container_output,
        ])

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds + 5.0,
                text=True,
            )
        finally:
            # Guarantee container removal even on timeout, cancellation, or crash
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        output_file = job_dir / "worker_output.json"
        if proc.returncode != 0 or not output_file.is_file():
            out_content = output_file.read_text(encoding="utf-8") if output_file.is_file() else "NO_FILE"
            raise RuntimeError(
                f"Docker worker failed (exit {proc.returncode}): output={out_content!r} stderr={proc.stderr!r} stdout={proc.stdout!r}"
            )
        return json.loads(output_file.read_text(encoding="utf-8"))


class BrowserWorkerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the browser worker daemon."""

    server_version = "SmartFetchWorker/1.11.1"

    def log_message(self, format: str, *args: Any) -> None:
        _LOGGER.info(f"%s - - [{self.log_date_time_string()}] {format % args}")

    @property
    def daemon_server(self) -> BrowserDaemonServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        if self.path == "/health":
            active_jobs = self.daemon_server.active_count
            payload = {
                "ok": True,
                "service": "smartfetch-browser-worker",
                "version": "1.11.1",
                "active_jobs": active_jobs,
                "capacity": self.daemon_server.config.max_concurrent,
            }
            self._send_json(200, payload)
            return

        self._send_json(404, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/render":
            self._send_json(404, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Not found"})
            return

        # 1. Read and bound body
        content_length_header = self.headers.get("Content-Length")
        if not content_length_header:
            self._send_json(400, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Missing Content-Length"})
            return

        try:
            length = int(content_length_header)
        except ValueError:
            self._send_json(400, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Invalid Content-Length"})
            return

        if length > self.daemon_server.config.max_request_bytes:
            self._send_json(413, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Request body too large"})
            return

        body_bytes = self.rfile.read(length)
        if len(body_bytes) != length:
            self._send_json(400, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Incomplete body read"})
            return

        # 2. Authenticate HMAC signature
        hmac_key = self.daemon_server.config.hmac_key
        if not hmac_key:
            self._send_json(500, {"success": False, "error_code": ERROR_BROWSER_UNAVAILABLE, "error": "Worker HMAC key not configured"})
            return

        header_dict = {k: v for k, v in self.headers.items()}
        if not verify_signature("POST", "/v1/render", body_bytes, hmac_key, header_dict):
            self._send_json(401, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Invalid HMAC signature or timestamp"})
            return

        nonce = self.headers.get("X-SmartFetch-Nonce") or self.headers.get("x-smartfetch-nonce")
        if not nonce or not self.daemon_server.nonce_cache.check_and_add(nonce):
            self._send_json(401, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Replayed or missing nonce"})
            return

        # 3. Parse JSON schema
        try:
            req_data = json.loads(body_bytes.decode("utf-8"))
            if not isinstance(req_data, dict):
                raise ValueError("Expected JSON object")
        except Exception as exc:
            self._send_json(400, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": f"Invalid JSON: {exc}"})
            return

        url = req_data.get("url")
        if not isinstance(url, str) or not url.strip() or len(url) > 4096:
            self._send_json(400, {"success": False, "error_code": ERROR_INVALID_REQUEST, "error": "Invalid URL parameter"})
            return

        timeout_seconds = float(req_data.get("timeout_seconds", 15.0))
        timeout_seconds = max(1.0, min(timeout_seconds, 25.0))
        max_chars = int(req_data.get("max_chars", 20000))
        max_chars = max(1000, min(max_chars, 50000))

        # 4. Enforce strict destination policy (anti-SSRF / cloud metadata blocking)
        try:
            safe_url = self.daemon_server.url_validator(url.strip())
        except (DNSResolutionError, ValueError) as exc:
            self._send_json(403, {
                "success": False,
                "error_code": ERROR_POLICY_REJECTION,
                "phase": "connect",
                "error": f"Target destination rejected by policy: {exc}",
            })
            return

        # 5. Acquire capacity semaphore
        acquired = self.daemon_server.semaphore.acquire(blocking=False)
        if not acquired:
            self._send_json(503, {
                "success": False,
                "error_code": ERROR_BROWSER_UNAVAILABLE,
                "phase": "capacity",
                "error": "Worker capacity busy; retry shortly",
            })
            return

        try:
            self.daemon_server._active_jobs += 1
            self._handle_render_job(safe_url, timeout_seconds=timeout_seconds, max_chars=max_chars)
        finally:
            self.daemon_server._active_jobs -= 1
            self.daemon_server.semaphore.release()

    def _handle_render_job(self, url: str, *, timeout_seconds: float, max_chars: int) -> None:
        started = time.monotonic()
        deadline = started + timeout_seconds
        operation_id = secrets.token_bytes(16)
        job_id = secrets.token_hex(8)

        ipc_root = Path(self.daemon_server.config.ipc_dir)
        job_dir = ipc_root / f"job-{job_id}"
        socket_path = job_dir / "relay.sock"

        broker = None
        bridge = None
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            # Try to fix permissions for UID 10001
            try:
                os.chmod(job_dir, 0o700)
                if hasattr(os, "chown") and os.geteuid() == 0:
                    os.chown(job_dir, 10001, 10001)
            except OSError:
                pass

            # Start Broker with strict public DestinationPolicy
            broker = Broker(DestinationPolicy(), total_deadline=deadline)
            broker_address = broker.start()

            # Start BridgeServer tied to this job's Unix socket
            if hasattr(socket, "AF_UNIX"):
                allowed_uid = 10001 if os.name != "nt" else None
                bridge = BridgeServer(
                    str(socket_path),
                    broker_address,
                    operation_id=operation_id,
                    deadline=deadline,
                    allowed_uid=allowed_uid,
                )
                bridge.start()

                # Make socket accessible to UID 10001
                try:
                    os.chmod(socket_path, 0o600)
                    if hasattr(os, "chown") and os.geteuid() == 0:
                        os.chown(socket_path, 10001, 10001)
                except OSError:
                    pass

            # Execute worker runner
            result = self.daemon_server.runner.run(
                url,
                socket_path=str(socket_path),
                operation_id=operation_id,
                deadline=deadline,
                max_chars=max_chars,
                timeout_seconds=timeout_seconds,
            )

            elapsed_ms = round((time.monotonic() - started) * 1000)

            # Build success response envelope matching Phase 2A schema
            response_payload = {
                "success": True,
                "requested_url": url,
                "final_url": result.get("final_url", url),
                "status_code": result.get("status_code", 200),
                "render_method": "browser",
                "elapsed_ms": elapsed_ms,
                "content": result.get("content", ""),
                "title": result.get("title"),
                "description": result.get("description"),
                "markdown": result.get("markdown", ""),
                "html": result.get("html", ""),
                "diagnostics": {
                    "worker_elapsed_ms": result.get("elapsed_ms", elapsed_ms),
                    "broker_bytes": broker.operation_bytes,
                    "broker_requests": broker.operation_requests,
                },
            }
            self._send_json(200, response_payload)

        except TimeoutError:
            self._send_json(504, {
                "success": False,
                "error_code": ERROR_TIMEOUT,
                "phase": "browser_navigate",
                "error": f"Rendering exceeded deadline of {timeout_seconds}s",
            })
        except WorkerBoundaryError as exc:
            self._send_json(500, {
                "success": False,
                "error_code": ERROR_RENDER_FAILED,
                "phase": "browser_start",
                "error": f"Worker boundary assertion failed: {exc.code}",
            })
        except PolicyError as exc:
            self._send_json(403, {
                "success": False,
                "error_code": ERROR_POLICY_REJECTION,
                "phase": "connect",
                "error": f"Broker policy error: {exc.code}",
            })
        except Exception as exc:
            _LOGGER.exception(f"Render job failed for {url}: {exc}")
            self._send_json(500, {
                "success": False,
                "error_code": ERROR_RENDER_FAILED,
                "phase": "browser_navigate",
                "error": f"Browser execution failed: {exc}",
            })
        finally:
            if bridge is not None:
                bridge.close()
            if broker is not None:
                broker.close()
            if job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class BrowserDaemonServer(ThreadingHTTPServer):
    """Threaded HTTP server for the browser worker daemon."""

    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        config: BrowserDaemonConfig,
        runner: WorkerRunner,
        nonce_cache: NonceReplayCache,
        url_validator: Any = None,
    ):
        self.config = config
        self.runner = runner
        self.nonce_cache = nonce_cache
        self.url_validator = url_validator if url_validator is not None else validate_public_url
        self.semaphore = threading.BoundedSemaphore(config.max_concurrent)
        self._active_jobs = 0
        super().__init__((config.host, config.port), BrowserWorkerHandler)

    @property
    def active_count(self) -> int:
        return self._active_jobs


# Alias for caller ergonomics
BrowserWorkerDaemon = BrowserDaemonServer


def create_daemon(
    config: BrowserDaemonConfig | None = None,
    runner: WorkerRunner | None = None,
    url_validator: Any = None,
) -> BrowserDaemonServer:
    """Factory creating and configuring the BrowserWorkerDaemon."""
    cfg = config or BrowserDaemonConfig.from_env()
    nonce_cache = NonceReplayCache()

    if runner is None:
        if cfg.worker_mode == "docker":
            runner = DockerWorkerRunner(cfg.docker_image, cfg.seccomp_profile, ipc_volume=cfg.ipc_volume)
        elif cfg.worker_mode == "subprocess":
            runner = SubprocessWorkerRunner(cfg.chromium_path)
        else:
            runner = DirectWorkerRunner(cfg.chromium_path)

    server = BrowserDaemonServer(cfg, runner, nonce_cache, url_validator=url_validator)

    # Wrap in TLS if cert and key are provided
    if cfg.ssl_cert and cfg.ssl_key:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(certfile=cfg.ssl_cert, keyfile=cfg.ssl_key)
        if cfg.ssl_ca:
            ctx.load_verify_locations(cafile=cfg.ssl_ca)
            ctx.verify_mode = ssl.CERT_REQUIRED
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    return server


def main() -> int:
    config = BrowserDaemonConfig.from_env()
    if not config.hmac_key:
        sys.stderr.write("ERROR: BROWSER_WORKER_HMAC_KEY must be set.\n")
        return 1

    server = create_daemon(config)
    print(f"SmartFetch Browser Worker Daemon listening on {config.host}:{config.port} (mode={config.worker_mode})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
