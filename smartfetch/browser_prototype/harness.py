"""Disabled local browser harness; not imported by production dispatch."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import websocket

from smartfetch.extract import extract_content
from smartfetch.limits import normalize_max_chars, shape_output


class PrototypeFailure(RuntimeError):
    """Finite, privacy-safe local-prototype failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _minimal_environment(profile: str) -> dict[str, str]:
    home = Path(profile) / "home"
    home.mkdir()
    if os.name == "nt":
        roaming = home / "AppData" / "Roaming"
        local = home / "AppData" / "Local"
        roaming.mkdir(parents=True)
        local.mkdir(parents=True)
        allowed = ("SystemRoot", "WINDIR", "PATH", "COMSPEC", "TEMP", "TMP")
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        environment["USERPROFILE"] = str(home)
        environment["APPDATA"] = str(roaming)
        environment["LOCALAPPDATA"] = str(local)
        return environment
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": profile,
            "LANG": "C.UTF-8"}


def _read_local_json(url: str, *, method: str = "GET", timeout: float) -> dict:
    request = Request(url, method=method)
    with urlopen(request, timeout=timeout) as response:
        data = response.read(65_537)
    if len(data) > 65_536:
        raise PrototypeFailure("browser_protocol")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise PrototypeFailure("browser_protocol")
    return value


def _cdp_call(ws, message_id: int, method: str, params: dict,
              work_deadline: float) -> dict:
    remaining = work_deadline - time.monotonic()
    if remaining <= 0:
        raise PrototypeFailure("browser_timeout")
    ws.settimeout(remaining)
    ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
    while time.monotonic() < work_deadline:
        ws.settimeout(work_deadline - time.monotonic())
        raw = ws.recv()
        if not isinstance(raw, str) or len(raw) > 2_000_000:
            raise PrototypeFailure("browser_protocol")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("id") != message_id:
            continue
        if "error" in value or not isinstance(value.get("result"), dict):
            raise PrototypeFailure("browser_protocol")
        return value["result"]
    raise PrototypeFailure("browser_timeout")


def _evaluate(ws, message_id: int, expression: str, work_deadline: float):
    result = _cdp_call(ws, message_id, "Runtime.evaluate", {
        "expression": expression, "returnByValue": True,
    }, work_deadline)
    if "exceptionDetails" in result:
        raise PrototypeFailure("browser_protocol")
    return result.get("result", {}).get("value")


def _stop_windows_tree(proc: subprocess.Popen, deadline: float) -> bool:
    """Terminate observed descendants using process handles, not root-PID exit.

    This is local Windows functional-test cleanup, not a production isolation
    primitive or an independent proof against descendants escaping the tree.
    """
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent_pid", wintypes.DWORD), ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def snapshot():
        handle = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
        if handle == ctypes.c_void_p(-1).value:
            return None
        entries = []
        try:
            entry = ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            if kernel.Process32FirstW(handle, ctypes.byref(entry)):
                while True:
                    entries.append((int(entry.pid), int(entry.parent_pid)))
                    if not kernel.Process32NextW(handle, ctypes.byref(entry)):
                        break
        finally:
            kernel.CloseHandle(handle)
        return entries

    known = {proc.pid}
    handles = {}
    confirmed = False
    try:
        while True:
            entries = snapshot()
            if entries is None:
                break
            present = {pid for pid, _ in entries}
            while True:
                expanded = known | {pid for pid, parent in entries if parent in known}
                if expanded == known:
                    break
                known = expanded
            for pid in known & present:
                if pid in handles:
                    continue
                handle = kernel.OpenProcess(0x00100001, False, pid)
                if not handle:
                    if ctypes.get_last_error() == 87:  # exited during snapshot
                        continue
                    return False
                handles[pid] = handle
            all_exited = True
            # Chrome may deny direct termination of a sandboxed child. Stop
            # the browser root first, then require every captured child handle
            # to signal exit; a denial is not itself proof of surviving work.
            for pid in sorted(handles, key=lambda value: value != proc.pid):
                handle = handles[pid]
                state = kernel.WaitForSingleObject(handle, 0)
                if state == 0:
                    continue
                all_exited = False
                if state != 0x102:
                    return False
                if pid == proc.pid:
                    try:
                        proc.kill()  # use the original Popen process handle
                    except OSError:
                        if kernel.WaitForSingleObject(handle, 0) != 0:
                            return False
                else:
                    # A Chrome sandboxed child can deny direct termination;
                    # its identity-bound handle must still signal exit.
                    kernel.TerminateProcess(handle, 1)
            if all_exited and not (known & present):
                try:
                    proc.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    break
                confirmed = time.monotonic() <= deadline
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    finally:
        for handle in handles.values():
            kernel.CloseHandle(handle)
        if not confirmed and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass  # return uncertain; never report an unverified exit
    return confirmed


def _stop_process(proc: subprocess.Popen, deadline: float) -> bool:
    if os.name == "nt":
        return _stop_windows_tree(proc, deadline)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=max(0.1, min(0.5, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return proc.poll() is not None


def _remove_profile(profile: str, deadline: float) -> bool:
    resolved = Path(profile).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if resolved.parent != temp_root or not resolved.name.startswith("smartfetch-prototype-"):
        return False
    while time.monotonic() < deadline:
        try:
            shutil.rmtree(resolved)
            return not resolved.exists()
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return False


def render_local(url: str, *, chromium: str, proxy_host: str, proxy_port: int,
                 deadline: float, max_chars: int) -> dict:
    if proxy_host != "127.0.0.1" or not (1 <= proxy_port <= 65535):
        raise PrototypeFailure("proxy_required")
    if not os.path.isfile(chromium):
        raise PrototypeFailure("browser_unavailable")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username is not None or parsed.password is not None):
            raise PrototypeFailure("invalid_target")
        max_chars = normalize_max_chars(max_chars)
    except (ValueError, TypeError):
        raise PrototypeFailure("invalid_target") from None
    started = time.monotonic()
    work_deadline = deadline - 3.0
    if time.monotonic() >= work_deadline:
        raise PrototypeFailure("browser_timeout")
    profile = tempfile.mkdtemp(prefix="smartfetch-prototype-")
    browser_data = Path(profile) / "chrome-data"
    browser_data.mkdir()
    process = None
    ws = None
    result = None
    failure = None
    try:
        command = [
            chromium, "--headless=new", "--disable-gpu",
            "--disable-background-networking", "--disable-component-update",
            "--disable-extensions", "--disable-default-apps", "--disable-sync",
            "--no-first-run", "--no-default-browser-check",
            f"--proxy-server=http://{proxy_host}:{proxy_port}",
            "--proxy-bypass-list=<-loopback>",
            "--remote-debugging-port=0",
            "--remote-allow-origins=http://127.0.0.1",
            f"--user-data-dir={browser_data}", "about:blank",
        ]
        process = subprocess.Popen(
            command, env=_minimal_environment(profile),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        port_file = browser_data / "DevToolsActivePort"
        port = None
        while time.monotonic() < work_deadline:
            if process.poll() is not None:
                raise PrototypeFailure("browser_start")
            if port_file.is_file():
                try:
                    lines = port_file.read_text(encoding="ascii").splitlines()
                except (PermissionError, FileNotFoundError):
                    # Chrome can briefly hold the file exclusively on Windows,
                    # or replace it between the existence check and the read.
                    lines = []
                if lines and lines[0].isdigit():
                    port = int(lines[0])
                    break
            time.sleep(0.05)
        if port is None:
            raise PrototypeFailure("browser_timeout")
        base = f"http://127.0.0.1:{port}"
        remaining = work_deadline - time.monotonic()
        if remaining <= 0:
            raise PrototypeFailure("browser_timeout")
        target = _read_local_json(base + "/json/new?about:blank", method="PUT",
                                  timeout=remaining)
        ws_url = target.get("webSocketDebuggerUrl")
        if not isinstance(ws_url, str) or not ws_url.startswith(
                f"ws://127.0.0.1:{port}/"):
            raise PrototypeFailure("browser_protocol")
        remaining = work_deadline - time.monotonic()
        if remaining <= 0:
            raise PrototypeFailure("browser_timeout")
        ws = websocket.create_connection(
            ws_url, timeout=remaining,
            origin="http://127.0.0.1",
        )
        _cdp_call(ws, 1, "Page.enable", {}, work_deadline)
        navigation = _cdp_call(ws, 2, "Page.navigate", {"url": url}, work_deadline)
        if navigation.get("errorText"):
            raise PrototypeFailure("navigation_failed")
        for message_id in range(3, 100):
            ready = _evaluate(ws, message_id, "document.readyState", work_deadline)
            if ready == "complete":
                break
            time.sleep(min(0.05, max(0, work_deadline - time.monotonic())))
        else:
            raise PrototypeFailure("browser_timeout")
        time.sleep(min(0.2, max(0, work_deadline - time.monotonic())))
        html = _evaluate(
            ws, 100, "document.documentElement.outerHTML.slice(0,1000001)",
            work_deadline,
        )
        final_url = _evaluate(ws, 101, "location.href", work_deadline)
        status = _evaluate(ws, 102,
                           "(performance.getEntriesByType('navigation')[0] || {}).responseStatus || 200",
                           work_deadline)
        if not isinstance(html, str) or not html or len(html) > 1_000_000:
            raise PrototypeFailure("browser_output")
        if not isinstance(final_url, str) or len(final_url) > 2048:
            raise PrototypeFailure("browser_output")
        if not isinstance(status, int) or not 200 <= status < 400:
            raise PrototypeFailure("navigation_failed")
        extracted = extract_content(html, final_url)
        result = shape_output({
            "success": True, "requested_url": url, "final_url": final_url,
            "status_code": status, "render_method": "browser",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "fallback_reason": "forced", **extracted,
        }, max_chars)
    except PrototypeFailure as error:
        failure = error
    except (TimeoutError, websocket.WebSocketTimeoutException):
        failure = PrototypeFailure("browser_timeout")
    except Exception:
        failure = PrototypeFailure("browser_failure")
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        stopped = process is None or _stop_process(process, deadline)
        removed = _remove_profile(profile, deadline)
        if not stopped or not removed:
            failure = PrototypeFailure("cleanup_uncertain")
    if failure is not None:
        raise failure
    return result
