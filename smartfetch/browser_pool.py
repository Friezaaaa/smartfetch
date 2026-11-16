"""Browser Worker Pooling for SmartFetch.

Maintains warm headless Chromium instances to eliminate process startup latency.
Each job is executed inside a fresh incognito BrowserContext (Target.createBrowserContext)
and immediately destroyed upon completion (Target.disposeBrowserContext).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.request import Request, urlopen

import websocket

from smartfetch.extract import extract_content
from smartfetch.limits import normalize_max_chars, shape_output

_LOGGER = logging.getLogger("smartfetch.browser_pool")

DEFAULT_POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "1"))
MAX_JOBS_PER_INSTANCE = int(os.getenv("BROWSER_POOL_MAX_JOBS", "100"))
MAX_INSTANCE_AGE_SECONDS = float(os.getenv("BROWSER_POOL_MAX_AGE_SECONDS", "1800.0"))  # 30 minutes
MAX_INSTANCE_RSS_BYTES = int(os.getenv("BROWSER_POOL_MAX_RSS_BYTES", "536870912"))  # 512 MB


def get_process_rss(pid: int) -> int:
    """Returns memory Resident Set Size (RSS) in bytes for a given PID."""
    if not isinstance(pid, int) or pid <= 0:
        return 0
    # Linux /proc/<pid>/statm
    try:
        with open(f"/proc/{pid}/statm", "r", encoding="ascii") as f:
            parts = f.read().split()
            if len(parts) >= 2:
                page_size = getattr(os, "sysconf", lambda _: 4096)("SC_PAGE_SIZE")
                return int(parts[1]) * page_size
    except Exception:
        pass
    return 0


class PoolExhaustedError(RuntimeError):
    """Raised when all warm pool instances are busy or failing to start."""


@dataclass
class InstanceMetrics:
    jobs_executed: int = 0
    total_latency_ms: float = 0.0
    created_at: float = field(default_factory=time.monotonic)
    last_job_at: float = field(default_factory=time.monotonic)
    recycle_count: int = 0


class WarmChromiumInstance:
    """A persistent, pre-warmed headless Chromium process that services render jobs."""

    metrics: InstanceMetrics | None = None
    proc: subprocess.Popen | None = None
    ws_endpoint: str | None = None
    _tainted: bool = False
    is_busy: bool = False

    def __init__(
        self,
        instance_id: str,
        chromium_path: str = "/usr/bin/chromium",
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        max_jobs: int = MAX_JOBS_PER_INSTANCE,
        max_age: float = MAX_INSTANCE_AGE_SECONDS,
        max_rss: int = MAX_INSTANCE_RSS_BYTES,
    ) -> None:
        self.instance_id = instance_id
        self.chromium_path = chromium_path
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.max_jobs = max_jobs
        self.max_age = max_age
        self.max_rss = max_rss

        self.proc: subprocess.Popen | None = None
        self.profile_dir: str | None = None
        self.ws_endpoint: str | None = None
        self.port: int | None = None
        self.is_busy: bool = False
        self._tainted: bool = False
        self.metrics = InstanceMetrics()
        self._lock = threading.Lock()

    def start(self, timeout: float = 10.0) -> None:
        """Launches Chromium in headless mode with remote debugging enabled."""
        self.profile_dir = tempfile.mkdtemp(prefix="sf_warm_profile_")
        flags = [
            self.chromium_path,
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--disable-translate",
            "--metrics-recording-only",
            "--mute-audio",
            "--safebrowsing-disable-auto-update",
            "--remote-debugging-port=0",
            f"--user-data-dir={self.profile_dir}",
            "about:blank",
        ]
        if self.proxy_host and self.proxy_port:
            flags.insert(1, f"--proxy-server=http://{self.proxy_host}:{self.proxy_port}")
            flags.insert(2, "--proxy-bypass-list=<-loopback>")

        # Start child process
        self.proc = subprocess.Popen(
            flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Parse DevTools active port from profile or stderr
        deadline = time.monotonic() + timeout
        port_file = Path(self.profile_dir) / "DevToolsActivePort"
        while time.monotonic() < deadline:
            if port_file.is_file():
                try:
                    lines = port_file.read_text(encoding="utf-8").splitlines()
                    if len(lines) >= 2:
                        self.port = int(lines[0].strip())
                        browser_path = lines[1].strip()
                        self.ws_endpoint = f"ws://127.0.0.1:{self.port}{browser_path}"
                        break
                except Exception:
                    pass
            if self.proc.poll() is not None:
                err = self.proc.stderr.read() if self.proc.stderr else "exit"
                raise RuntimeError(f"Warm Chromium process exited prematurely: {err}")
            time.sleep(0.05)

        if not self.ws_endpoint:
            self.close()
            raise RuntimeError(f"Warm Chromium failed to produce DevToolsActivePort within {timeout}s")

        self.metrics = InstanceMetrics()
        _LOGGER.info(
            "Warm Chromium instance %s started on port %d (PID %d)",
            self.instance_id,
            self.port or 0,
            self.proc.pid,
        )

    def is_healthy(self) -> bool:
        """Determines if the warm instance is alive, responsive, and within resource bounds."""
        if self._tainted:
            return False
        if self.proc is None or self.proc.poll() is not None:
            return False
        now = time.monotonic()
        if self.metrics.jobs_executed >= self.max_jobs:
            return False
        if (now - self.metrics.created_at) >= self.max_age:
            return False
        rss = get_process_rss(self.proc.pid)
        if rss > 0 and rss >= self.max_rss:
            _LOGGER.warning(
                "Warm Chromium instance %s exceeded RSS ceiling (%d > %d bytes)",
                self.instance_id,
                rss,
                self.max_rss,
            )
            return False
        return True

    def close(self) -> None:
        """Terminates the Chromium process and purges the profile directory."""
        if self.proc:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
            except Exception as exc:
                _LOGGER.debug("Error stopping warm Chromium instance: %s", exc)
            self.proc = None

        if self.profile_dir and os.path.isdir(self.profile_dir):
            try:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            except Exception:
                pass
            self.profile_dir = None

    def render(
        self,
        url: str,
        *,
        deadline: float,
        max_chars: int = 20000,
        wait_ready: bool = False,
    ) -> dict[str, Any]:
        """Renders the given URL in a dedicated ephemeral BrowserContext."""
        if not self.ws_endpoint:
            raise RuntimeError("Warm Chromium instance is not running")

        start_time = time.monotonic()
        remaining = deadline - start_time
        if remaining <= 0:
            raise TimeoutError("Deadline exceeded before browser acquisition")

        # Connect to browser DevTools WebSocket
        ws = websocket.create_connection(self.ws_endpoint, timeout=min(5.0, remaining))
        msg_id = 0

        def cdp_send(method: str, params: dict | None = None) -> dict:
            nonlocal msg_id
            msg_id += 1
            call_id = msg_id
            payload = {"id": call_id, "method": method, "params": params or {}}
            ws.send(json.dumps(payload))
            while True:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("Browser operation timed out")
                ws.settimeout(rem)
                raw = ws.recv()
                data = json.loads(raw)
                if data.get("id") == call_id:
                    if "error" in data:
                        raise RuntimeError(f"CDP call {method} error: {data['error']}")
                    return data.get("result", {})

        context_id = None
        target_id = None
        session_id = None
        try:
            # 1. Create fresh ephemeral incognito BrowserContext
            ctx_res = cdp_send("Target.createBrowserContext")
            context_id = ctx_res["browserContextId"]

            # 2. Create blank target page inside this context
            target_res = cdp_send(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": context_id},
            )
            target_id = target_res["targetId"]

            # 3. Attach to the page target to obtain a sessionId
            attach_res = cdp_send(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = attach_res["sessionId"]

            # 4. Helper to send commands scoped to this page session
            def page_send(method: str, params: dict | None = None) -> dict:
                nonlocal msg_id
                msg_id += 1
                call_id = msg_id
                payload = {
                    "id": call_id,
                    "sessionId": session_id,
                    "method": method,
                    "params": params or {},
                }
                ws.send(json.dumps(payload))
                while True:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        raise TimeoutError("Browser operation timed out")
                    ws.settimeout(rem)
                    raw = ws.recv()
                    data = json.loads(raw)
                    if data.get("id") == call_id:
                        if "error" in data:
                            raise RuntimeError(f"Page call {method} error: {data['error']}")
                        return data.get("result", {})

            page_send("Page.enable")
            page_send("Page.navigate", {"url": url})

            status_code = 200
            if wait_ready:
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                    try:
                        ready_eval = page_send(
                            "Runtime.evaluate",
                            {"expression": "document.readyState", "returnByValue": True},
                        )
                        ready = ready_eval.get("result", {}).get("value")
                        if ready in {"interactive", "complete"}:
                            break
                    except Exception:
                        pass
                time.sleep(0.3)
                try:
                    status_eval = page_send(
                        "Runtime.evaluate",
                        {
                            "expression": '(performance.getEntriesByType("navigation")[0] && performance.getEntriesByType("navigation")[0].responseStatus) || 200',
                            "returnByValue": True,
                        },
                    )
                    raw_status = status_eval.get("result", {}).get("value", 200)
                    status_code = int(raw_status or 200)
                except Exception:
                    status_code = 200

                if status_code >= 400:
                    raise RuntimeError(f"Browser navigation returned HTTP {status_code}")
            else:
                # Wait briefly for document rendering or navigation completion
                time.sleep(0.5)

            # 5. Extract rendered HTML and document metadata
            html_eval = page_send(
                "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML", "returnByValue": True},
            )
            raw_html = html_eval.get("result", {}).get("value", "")

            url_eval = page_send(
                "Runtime.evaluate",
                {"expression": "window.location.href", "returnByValue": True},
            )
            final_url = url_eval.get("result", {}).get("value", url)

            extracted = extract_content(raw_html, final_url)
            elapsed_ms = round((time.monotonic() - start_time) * 1000)

            # Update metrics
            with self._lock:
                self.metrics.jobs_executed += 1
                self.metrics.total_latency_ms += elapsed_ms
                self.metrics.last_job_at = time.monotonic()

            payload = {
                "success": True,
                "requested_url": url,
                "final_url": final_url,
                "status_code": status_code,
                "render_method": "browser",
                "retrieval_tier": "browser",
                "cache_hit": False,
                "elapsed_ms": elapsed_ms,
                **extracted,
            }
            if wait_ready:
                payload["html"] = raw_html

            return shape_output(payload, max_chars)

        except Exception as exc:
            self._tainted = True
            _LOGGER.warning("Warm Chromium instance %s marked tainted due to error: %s", self.instance_id, exc)
            raise
        finally:
            # 6. Dispose the BrowserContext to wipe all cookies, storage, and page memory
            if context_id:
                try:
                    cdp_send("Target.disposeBrowserContext", {"browserContextId": context_id})
                except Exception:
                    pass
            try:
                ws.close()
            except Exception:
                pass


class BrowserPoolManager:
    """Manages the pool of warm Chromium instances with automated recycling."""

    def __init__(
        self,
        pool_size: int = DEFAULT_POOL_SIZE,
        chromium_path: str = "/usr/bin/chromium",
    ) -> None:
        self.pool_size = max(1, pool_size)
        self.chromium_path = chromium_path
        self._instances: list[WarmChromiumInstance] = []
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._launching_count = 0
        self._recycle_counter = 0

    def acquire(self, timeout: float = 10.0) -> WarmChromiumInstance:
        """Returns an idle, healthy warm instance from the pool within pool_size limit.

        If all instances are busy, waits up to timeout seconds for an instance to become free.
        Raises PoolExhaustedError if capacity is exhausted and no instance becomes available.
        """
        deadline = time.monotonic() + max(0.1, timeout)
        while True:
            instance_to_start = None
            with self._condition:
                # 1. Clean up unhealthy idle instances
                for inst in list(self._instances):
                    if not inst.is_busy and not inst.is_healthy():
                        _LOGGER.info("Retiring unhealthy warm Chromium instance %s", inst.instance_id)
                        inst.close()
                        self._instances.remove(inst)
                        self._recycle_counter += 1

                # 2. Look for an idle, healthy instance
                for inst in self._instances:
                    if not inst.is_busy and inst.is_healthy():
                        inst.is_busy = True
                        return inst

                # 3. Check if we have capacity under pool_size to start a new instance
                total_allocated = len(self._instances) + self._launching_count
                if total_allocated < self.pool_size:
                    self._launching_count += 1
                    new_id = f"inst-{len(self._instances) + self._launching_count}-{int(time.monotonic())}"
                    instance_to_start = WarmChromiumInstance(
                        instance_id=new_id,
                        chromium_path=self.chromium_path,
                    )
                else:
                    # At capacity! Must wait for an instance to be released
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        raise PoolExhaustedError(
                            f"Browser pool capacity ({self.pool_size}) exhausted; timed out waiting for an available instance."
                        )
                    self._condition.wait(timeout=rem)

            # If we need to start an instance, do it outside the lock
            if instance_to_start is not None:
                try:
                    start_timeout = min(10.0, max(1.0, deadline - time.monotonic()))
                    instance_to_start.start(timeout=start_timeout)
                    instance_to_start.is_busy = True
                    with self._condition:
                        self._launching_count -= 1
                        self._instances.append(instance_to_start)
                    return instance_to_start
                except Exception:
                    with self._condition:
                        self._launching_count -= 1
                        self._condition.notify_all()
                    raise

    def release(self, instance: WarmChromiumInstance) -> None:
        """Releases an instance back to the pool."""
        with self._condition:
            instance.is_busy = False
            # Check if it should be recycled immediately
            if not instance.is_healthy():
                _LOGGER.info("Recycling unhealthy warm Chromium instance %s", instance.instance_id)
                instance.close()
                if instance in self._instances:
                    self._instances.remove(instance)
                self._recycle_counter += 1
            self._condition.notify()

    def close_all(self) -> None:
        """Terminates all instances in the pool."""
        with self._condition:
            for inst in self._instances:
                inst.close()
            self._instances.clear()
            self._condition.notify_all()

    def get_status(self) -> dict[str, Any]:
        """Returns diagnostic metrics on the pool."""
        with self._condition:
            active_count = sum(1 for i in self._instances if i.is_busy)
            total_jobs = sum(i.metrics.jobs_executed for i in self._instances)
            rss_total = sum(get_process_rss(i.proc.pid) for i in self._instances if i.proc)
            return {
                "pool_size_target": self.pool_size,
                "instances_alive": len(self._instances),
                "instances_busy": active_count,
                "launching_count": self._launching_count,
                "recycles_count": self._recycle_counter,
                "total_jobs_served": total_jobs,
                "total_memory_rss_bytes": rss_total,
            }


# Global active browser pool manager
_ACTIVE_POOL: BrowserPoolManager | None = None


def get_browser_pool(chromium_path: str = "/usr/bin/chromium") -> BrowserPoolManager:
    global _ACTIVE_POOL
    if _ACTIVE_POOL is None:
        _ACTIVE_POOL = BrowserPoolManager(pool_size=DEFAULT_POOL_SIZE, chromium_path=chromium_path)
    return _ACTIVE_POOL
