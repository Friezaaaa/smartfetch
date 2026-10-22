"""Local-only browser prototype tests; never run against external origins."""

import time
import os
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import websocket

from smartfetch.browser_prototype.broker import Broker
from smartfetch.browser_prototype.harness import (
    PrototypeFailure, _minimal_environment, _stop_process, render_local,
)
import smartfetch.browser_prototype.harness as browser_harness
from smartfetch.browser_prototype.policy import DestinationPolicy


class BrowserHarnessBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process-tree diagnostic")
    def test_cleanup_stops_child_even_when_taskkill_stalls(self):
        child_program = "import time; time.sleep(30)"
        parent_program = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL); "
            "print(child.pid,flush=True); time.sleep(30)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_program, child_program],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        child_pid = int(parent.stdout.readline().strip())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]

        def child_exited():
            handle = kernel.OpenProcess(0x00100000, 0, child_pid)
            if not handle:
                return True
            try:
                return kernel.WaitForSingleObject(handle, 0) == 0
            finally:
                kernel.CloseHandle(handle)

        try:
            self.assertFalse(child_exited())
            with mock.patch.object(
                browser_harness.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("taskkill", 2),
            ):
                stopped = _stop_process(parent, time.monotonic() + 3)
            self.assertTrue(stopped)
            self.assertTrue(child_exited(), "a live descendant must not count as cleanup")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=3)
            if not child_exited():
                os.kill(child_pid, 15)
            parent.stdout.close()

    @unittest.skipUnless(os.name == "nt", "Windows cleanup failure path")
    def test_unconfirmed_process_exit_remains_finite_cleanup_failure(self):
        class UnkillableProcess:
            pid = 2_147_483_647  # No process is launched for this failure path.

            def poll(self):
                return None

            def wait(self, timeout):
                raise subprocess.TimeoutExpired("synthetic process", timeout)

            def kill(self):
                raise PermissionError("synthetic denied termination")

        self.assertFalse(_stop_process(UnkillableProcess(), time.monotonic() + 0.1))

    @unittest.skipUnless(os.name == "nt", "Windows Chrome layout diagnostic")
    def test_fresh_windows_home_has_separate_writable_appdata_and_browser_data(self):
        with tempfile.TemporaryDirectory(prefix="smartfetch-prototype-") as root:
            environment = _minimal_environment(root)
            home = Path(environment["USERPROFILE"])
            appdata = Path(environment["APPDATA"])
            localappdata = Path(environment["LOCALAPPDATA"])
            self.assertEqual(home.parent, Path(root))
            self.assertEqual(appdata, home / "AppData" / "Roaming")
            self.assertEqual(localappdata, home / "AppData" / "Local")
            self.assertTrue(all(path.is_dir() for path in (home, appdata, localappdata)))
            self.assertNotEqual(home, Path(root) / "chrome-data")
            self.assertNotIn("X402_TEST_SECRET", environment)

    def test_missing_proxy_fails_before_browser_launch(self):
        with self.assertRaises(PrototypeFailure) as raised:
            render_local(
                "http://fixture.test/story", chromium="missing-chrome",
                proxy_host="", proxy_port=0,
                deadline=time.monotonic() + 5, max_chars=1000,
            )
        self.assertEqual(raised.exception.code, "proxy_required")

    def test_missing_chromium_is_a_finite_failure(self):
        with self.assertRaises(PrototypeFailure) as raised:
            render_local(
                "http://fixture.test/story", chromium="missing-chrome",
                proxy_host="127.0.0.1", proxy_port=18765,
                deadline=time.monotonic() + 5, max_chars=1000,
            )
        self.assertEqual(raised.exception.code, "browser_unavailable")


class LocalFixturePolicy(DestinationPolicy):
    def port_allowed(self, scheme, port):
        return scheme == "http" and port == self.fixture_port

    def address_allowed(self, ip):
        return ip == "127.0.0.1"


class JavaScriptFixture(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        self.seen.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://fixture.test:{self.server.server_port}/story")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/stall":
            time.sleep(3)
        body = (
            b"<!doctype html><html><head><title>Rendered fixture</title></head>"
            b"<body><main><h1 id='dynamic'>Loading</h1>"
            b"<a href='/next'>Next page</a></main>"
            b"<script>setTimeout(() => { document.getElementById('dynamic')."
            b"textContent = 'JavaScript rendered the page'; }, 100);</script>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class FixtureServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler_errors = []

    def handle_error(self, _request, _client_address):
        # Forced cancellation closes fixture sockets; retain the bounded type
        # for verification without printing browser-controlled request data.
        self.handler_errors.append(sys.exc_info()[0])


class RealBrowserTests(unittest.TestCase):
    def setUp(self):
        self.chromium = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        if not __import__("os").path.isfile(self.chromium):
            self.skipTest("local Chrome unavailable")
        JavaScriptFixture.seen = []
        self.fixture = FixtureServer(("127.0.0.1", 0), JavaScriptFixture)
        self.fixture_thread = threading.Thread(target=self.fixture.serve_forever,
                                               daemon=True)
        self.fixture_thread.start()
        policy = LocalFixturePolicy()
        policy.fixture_port = self.fixture.server_port
        self.broker = Broker(
            policy,
            resolver=lambda _host, _port: ["127.0.0.1"],
            connector=lambda ip, port, timeout: socket.create_connection(
                (ip, port), timeout=timeout
            ),
            total_deadline=time.monotonic() + 20,
        )
        self.proxy_host, self.proxy_port = self.broker.start()

    def tearDown(self):
        if hasattr(self, "broker"):
            self.assertTrue(self.broker.close())
        if hasattr(self, "fixture"):
            self.fixture.shutdown()
            self.fixture.server_close()
            self.fixture_thread.join(timeout=2)
            self.assertTrue(all(
                error in {ConnectionResetError, ConnectionAbortedError, BrokenPipeError}
                for error in self.fixture.handler_errors
            ), [error.__name__ for error in self.fixture.handler_errors])

    def test_functional_only_javascript_page_preserves_browser_result_fields(self):
        requested = f"http://fixture.test:{self.fixture.server_port}/story"
        result = render_local(
            requested, chromium=self.chromium,
            proxy_host=self.proxy_host, proxy_port=self.proxy_port,
            deadline=time.monotonic() + 15, max_chars=1000,
        )
        self.assertEqual(result["requested_url"], requested)
        self.assertEqual(result["final_url"], requested)
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["render_method"], "browser")
        self.assertEqual(result["fallback_reason"], "forced")
        self.assertIn("JavaScript rendered the page", result["content"])
        self.assertIn("JavaScript rendered the page", result["markdown"])
        self.assertEqual(result["title"], "JavaScript rendered the page")
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        # Chrome may also request /favicon.ico; that is another broker admission.
        self.assertGreaterEqual(self.broker.allowed, 1)
        self.assertIn("/story", JavaScriptFixture.seen)

    def test_functional_only_slow_devtools_response_uses_shared_work_deadline(self):
        requested = f"http://fixture.test:{self.fixture.server_port}/story"
        original = browser_harness._read_local_json

        def delayed_response(url, *, method="GET", timeout):
            if timeout < 1.25:
                raise TimeoutError("synthetic slow DevTools response")
            time.sleep(1.1)
            return original(url, method=method, timeout=timeout - 1.1)

        with mock.patch.object(browser_harness, "_read_local_json", delayed_response):
            result = render_local(
                requested, chromium=self.chromium,
                proxy_host=self.proxy_host, proxy_port=self.proxy_port,
                deadline=time.monotonic() + 15, max_chars=1000,
            )
        self.assertEqual(result["render_method"], "browser")
        self.assertIn("JavaScript rendered the page", result["content"])

    def test_functional_only_transient_devtools_file_lock_is_polled(self):
        requested = f"http://fixture.test:{self.fixture.server_port}/story"
        original = Path.read_text
        blocked = False

        def locked_once(path, *args, **kwargs):
            nonlocal blocked
            if path.name == "DevToolsActivePort" and not blocked:
                blocked = True
                raise PermissionError("synthetic temporary Chrome file lock")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", locked_once):
            result = render_local(
                requested, chromium=self.chromium,
                proxy_host=self.proxy_host, proxy_port=self.proxy_port,
                deadline=time.monotonic() + 15, max_chars=1000,
            )
        self.assertEqual(result["render_method"], "browser")
        self.assertTrue(blocked)

    def test_functional_only_slow_devtools_websocket_uses_shared_work_deadline(self):
        requested = f"http://fixture.test:{self.fixture.server_port}/story"
        original = browser_harness.websocket.create_connection

        def delayed_connection(url, *, timeout, origin):
            if timeout < 2.25:
                raise websocket.WebSocketTimeoutException()
            time.sleep(2.1)
            return original(url, timeout=timeout - 2.1, origin=origin)

        with mock.patch.object(browser_harness.websocket, "create_connection",
                               delayed_connection):
            result = render_local(
                requested, chromium=self.chromium,
                proxy_host=self.proxy_host, proxy_port=self.proxy_port,
                deadline=time.monotonic() + 15, max_chars=1000,
            )
        self.assertEqual(result["render_method"], "browser")

    def test_functional_only_slow_cdp_reply_uses_shared_work_deadline(self):
        requested = f"http://fixture.test:{self.fixture.server_port}/story"
        original = websocket.WebSocket.recv
        delayed = False

        def delayed_reply(ws, *args, **kwargs):
            nonlocal delayed
            if not delayed:
                delayed = True
                if ws.gettimeout() < 2.25:
                    raise websocket.WebSocketTimeoutException()
                time.sleep(2.1)
            return original(ws, *args, **kwargs)

        with mock.patch.object(websocket.WebSocket, "recv", delayed_reply):
            result = render_local(
                requested, chromium=self.chromium,
                proxy_host=self.proxy_host, proxy_port=self.proxy_port,
                deadline=time.monotonic() + 15, max_chars=1000,
            )
        self.assertEqual(result["render_method"], "browser")

    def test_functional_only_redirect_reports_the_final_page(self):
        requested = f"http://fixture.test:{self.fixture.server_port}/redirect"
        result = render_local(
            requested, chromium=self.chromium,
            proxy_host=self.proxy_host, proxy_port=self.proxy_port,
            deadline=time.monotonic() + 15, max_chars=1000,
        )
        self.assertEqual(result["requested_url"], requested)
        self.assertEqual(result["final_url"],
                         f"http://fixture.test:{self.fixture.server_port}/story")
        self.assertIn("JavaScript rendered the page", result["content"])
        self.assertIn("/redirect", JavaScriptFixture.seen)
        self.assertIn("/story", JavaScriptFixture.seen)
        self.assertGreaterEqual(self.broker.allowed, 2)

    def test_functional_only_stalled_page_fails_by_shared_deadline(self):
        requested = f"http://fixture.test:{self.fixture.server_port}/stall"
        started = time.monotonic()
        with self.assertRaises(PrototypeFailure) as raised:
            render_local(
                requested, chromium=self.chromium,
                proxy_host=self.proxy_host, proxy_port=self.proxy_port,
                deadline=started + 5, max_chars=1000,
            )
        self.assertEqual(raised.exception.code, "browser_timeout")
        self.assertLess(time.monotonic() - started, 5.5)


if __name__ == "__main__":
    unittest.main()
