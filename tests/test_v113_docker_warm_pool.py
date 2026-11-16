"""Tests for v1.13 Step 4A: Docker Warm Pool Worker mode."""

import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from smartfetch.browser_daemon import (
    BrowserDaemonConfig,
    DockerWarmPoolRunner,
    _WarmWorkerSlot,
    create_daemon,
)
from smartfetch.browser_pool import (
    WarmChromiumInstance,
)
from smartfetch.browser_prototype.relay import DynamicWorkerRelay
from smartfetch.browser_prototype.worker import WorkerBoundaryError
from smartfetch.browser_worker import (
    process_control_job,
    serve_warm_worker,
)


class TestWarmChromiumInstanceDockerWarm(unittest.TestCase):
    """Verifies WarmChromiumInstance startup flags, wait_ready polling, and bounded HTML."""

    @patch("subprocess.Popen")
    @patch("smartfetch.browser_pool.Path.is_file", return_value=True)
    @patch("smartfetch.browser_pool.Path.read_text", return_value="9222\n/devtools/browser/abc\n")
    def test_startup_flags_include_dev_shm_and_proxy_bypass(self, mock_read, mock_isfile, mock_popen):
        inst = WarmChromiumInstance(
            instance_id="warm-flags-test",
            chromium_path="/usr/bin/chromium",
            proxy_host="127.0.0.1",
            proxy_port=8080,
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        inst.start(timeout=1.0)
        self.assertTrue(inst.ws_endpoint.startswith("ws://127.0.0.1:9222"))

        # Inspect flags passed to Popen
        flags = mock_popen.call_args[0][0]
        self.assertIn("--disable-dev-shm-usage", flags)
        self.assertIn("--proxy-server=http://127.0.0.1:8080", flags)
        self.assertIn("--proxy-bypass-list=<-loopback>", flags)

    @patch("smartfetch.browser_pool.websocket.create_connection")
    @patch("smartfetch.browser_pool.extract_content")
    def test_render_wait_ready_polls_and_includes_bounded_html(self, mock_extract, mock_ws_connect):
        inst = WarmChromiumInstance(instance_id="warm-render-test")
        inst.ws_endpoint = "ws://127.0.0.1:9222/devtools/browser/test"

        mock_ws = MagicMock()
        mock_ws_connect.return_value = mock_ws

        # Mock CDP message sequence for wait_ready=True
        responses = [
            # 1. Target.createBrowserContext
            '{"id": 1, "result": {"browserContextId": "ctx-warm-1"}}',
            # 2. Target.createTarget
            '{"id": 2, "result": {"targetId": "target-warm-2"}}',
            # 3. Target.attachToTarget
            '{"id": 3, "result": {"sessionId": "sess-warm-3"}}',
            # 4. Page.enable
            '{"id": 4, "result": {}}',
            # 5. Page.navigate
            '{"id": 5, "result": {}}',
            # 6. Runtime.evaluate (document.readyState)
            '{"id": 6, "result": {"result": {"value": "complete"}}}',
            # 7. Runtime.evaluate (responseStatus)
            '{"id": 7, "result": {"result": {"value": 200}}}',
            # 8. Runtime.evaluate (HTML)
            '{"id": 8, "result": {"result": {"value": "<html><body><h1>Warm Page</h1></body></html>"}}}',
            # 9. Runtime.evaluate (URL)
            '{"id": 9, "result": {"result": {"value": "https://example.com/warm"}}}',
            # 10. Target.disposeBrowserContext
            '{"id": 10, "result": {}}',
        ]
        mock_ws.recv.side_effect = responses
        mock_extract.return_value = {
            "title": "Warm Page",
            "content": "Warm Page Content",
            "markdown": "# Warm Page",
            "links": [],
            "low_quality": False,
        }

        res = inst.render(
            "https://example.com/warm",
            deadline=time.monotonic() + 5.0,
            max_chars=1000,
            wait_ready=True,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["status_code"], 200)
        self.assertIn("html", res)
        self.assertIn("Warm Page", res["html"])
        self.assertEqual(inst.metrics.jobs_executed, 1)

    @patch("smartfetch.browser_pool.websocket.create_connection")
    def test_render_wait_ready_raises_on_http_400_plus(self, mock_ws_connect):
        inst = WarmChromiumInstance(instance_id="warm-404-test")
        inst.ws_endpoint = "ws://127.0.0.1:9222/devtools/browser/test"

        mock_ws = MagicMock()
        mock_ws_connect.return_value = mock_ws

        responses = [
            '{"id": 1, "result": {"browserContextId": "ctx-warm-1"}}',
            '{"id": 2, "result": {"targetId": "target-warm-2"}}',
            '{"id": 3, "result": {"sessionId": "sess-warm-3"}}',
            '{"id": 4, "result": {}}',
            '{"id": 5, "result": {}}',
            # readyState: complete
            '{"id": 6, "result": {"result": {"value": "complete"}}}',
            # responseStatus: 403 Forbidden
            '{"id": 7, "result": {"result": {"value": 403}}}',
            '{"id": 8, "result": {}}',
        ]
        mock_ws.recv.side_effect = responses

        with self.assertRaises(RuntimeError) as ctx:
            inst.render(
                "https://example.com/blocked",
                deadline=time.monotonic() + 5.0,
                wait_ready=True,
            )
        self.assertIn("403", str(ctx.exception))
        self.assertTrue(inst._tainted)
        self.assertFalse(inst.is_healthy())


class TestDynamicWorkerRelay(unittest.TestCase):
    """Verifies DynamicWorkerRelay connection control, fail-closed handling, and job switching."""

    def test_relay_rejects_connections_when_no_job_active(self):
        relay = DynamicWorkerRelay(deadline=float("inf"))
        host, port = relay.start()
        try:
            sock = socket.create_connection((host, port), timeout=2.0)
            data = sock.recv(1024)
            # Must close immediately without error or data
            self.assertEqual(data, b"")
            sock.close()
        finally:
            relay.close()

    def test_relay_rejects_connections_when_deadline_expired(self):
        relay = DynamicWorkerRelay(deadline=float("inf"))
        host, port = relay.start()
        try:
            # Set job with past deadline
            relay.set_active_job("/tmp/test.sock", b"0123456789abcdef", deadline=time.monotonic() - 1.0)
            sock = socket.create_connection((host, port), timeout=2.0)
            data = sock.recv(1024)
            self.assertEqual(data, b"")
            sock.close()
        finally:
            relay.close()

    def test_relay_job_lifecycle_resets_counters_and_cleans_up(self):
        relay = DynamicWorkerRelay(deadline=float("inf"))
        try:
            relay.set_active_job("/tmp/job1.sock", b"0123456789abcdef", deadline=time.monotonic() + 10.0)
            self.assertIsNotNone(relay._active_job)
            self.assertEqual(relay.operation_bytes, 0)
            self.assertEqual(relay.admitted_sessions, 0)

            # Clear job
            relay.clear_active_job()
            self.assertIsNone(relay._active_job)
            self.assertEqual(len(relay._active_upstreams), 0)
        finally:
            relay.close()


class TestBrowserWorkerWarmServer(unittest.TestCase):
    """Verifies browser_worker.py warm server request processing and fail-closed behaviors."""

    def test_process_control_job_success(self):
        mock_relay = MagicMock()
        mock_instance = MagicMock()
        mock_instance.is_healthy.return_value = True
        mock_instance.render.return_value = {
            "title": "Success Title",
            "content": "Success Content",
            "status_code": 200,
            "html": "<html><body>Success</body></html>",
        }

        req = {
            "url": "https://example.com/test",
            "socket_path": "/tmp/job.sock",
            "operation_id": "0123456789abcdef0123456789abcdef",
            "deadline_seconds": 15.0,
            "max_chars": 5000,
        }

        resp = process_control_job(req, mock_relay, mock_instance)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["title"], "Success Title")
        self.assertFalse(resp["recycle_recommended"])

        mock_relay.set_active_job.assert_called_once()
        mock_instance.render.assert_called_once()
        mock_relay.clear_active_job.assert_called_once()

    def test_process_control_job_failure_flags_recycle_and_clears_relay(self):
        mock_relay = MagicMock()
        mock_instance = MagicMock()
        mock_instance.render.side_effect = RuntimeError("Navigation timeout")

        req = {
            "url": "https://example.com/fail",
            "socket_path": "/tmp/job.sock",
            "operation_id": "0123456789abcdef0123456789abcdef",
            "deadline_seconds": 5.0,
            "max_chars": 2000,
        }

        resp = process_control_job(req, mock_relay, mock_instance)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "render_failed")
        self.assertTrue(resp["recycle_recommended"])
        mock_relay.clear_active_job.assert_called_once()

    def test_serve_warm_worker_fails_closed_on_boundary_error(self):
        def failing_boundary():
            raise WorkerBoundaryError("worker_uid")

        with self.assertRaises(WorkerBoundaryError):
            serve_warm_worker(
                "/tmp/nonexistent.ctrl.sock",
                boundary_inspector=failing_boundary,
            )


class TestDockerWarmPoolRunner(unittest.TestCase):
    """Verifies DockerWarmPoolRunner container flags, exclusive concurrency, and auto-recycling."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="sf_warm_runner_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_daemon_wires_docker_warm_runner(self):
        cfg = BrowserDaemonConfig(
            worker_mode="docker_warm",
            ipc_dir=self.temp_dir,
            max_concurrent=2,
        )
        daemon = create_daemon(cfg)
        try:
            self.assertIsInstance(daemon.runner, DockerWarmPoolRunner)
            self.assertEqual(daemon.runner.max_concurrent, 2)
        finally:
            daemon.server_close()

    @patch("subprocess.run")
    def test_start_container_uses_strict_flags(self, mock_subproc):
        mock_subproc.return_value = MagicMock(returncode=0, stdout="started", stderr="")
        runner = DockerWarmPoolRunner(
            docker_image="smartfetch-worker:test",
            seccomp_profile="/tmp/nonexistent-seccomp.json",
            ipc_dir=self.temp_dir,
            max_concurrent=1,
        )

        slot = runner._slots[0]
        # Mock _connect_to_control_socket to avoid requiring real AF_UNIX container socket
        with patch.object(runner, "_connect_to_control_socket") as mock_conn:
            mock_conn.return_value = MagicMock()
            runner._start_container_for_slot(slot, deadline=time.monotonic() + 5.0)

        # Verify docker run flags
        calls = [c[0][0] for c in mock_subproc.call_args_list if c[0][0][0] == "docker" and c[0][0][1] == "run"]
        self.assertTrue(len(calls) > 0, "docker run was not called!")
        cmd = calls[0]

        self.assertIn("-d", cmd)
        self.assertIn("--rm", cmd)
        self.assertIn("--network", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")
        self.assertIn("--user", cmd)
        self.assertEqual(cmd[cmd.index("--user") + 1], "10001:10001")
        self.assertIn("--memory", cmd)
        self.assertEqual(cmd[cmd.index("--memory") + 1], "768m")
        self.assertIn("--cpus", cmd)
        self.assertEqual(cmd[cmd.index("--cpus") + 1], "1.0")
        self.assertIn("--pids-limit", cmd)
        self.assertEqual(cmd[cmd.index("--pids-limit") + 1], "256")
        self.assertIn("--warm-server", cmd)
        self.assertIn("--control-socket", cmd)

    def test_exclusive_slot_concurrency(self):
        runner = DockerWarmPoolRunner(
            ipc_dir=self.temp_dir,
            max_concurrent=2,
        )

        with patch.object(runner, "_start_container_for_slot", return_value="/mock/ctrl.sock"):
            with patch.object(runner, "_connect_to_control_socket", return_value=MagicMock()):
                with patch.object(runner, "_send_job_and_recv") as mock_send_recv:
                    mock_send_recv.return_value = {
                        "ok": True,
                        "result": {"status_code": 200, "content": "Done"},
                        "recycle_recommended": False,
                    }

                    res = runner.run(
                        "https://example.com",
                        socket_path=f"{self.temp_dir}/job-1/relay.sock",
                        operation_id=b"0123456789abcdef",
                        deadline=time.monotonic() + 5.0,
                        max_chars=2000,
                        timeout_seconds=5.0,
                    )
                    self.assertEqual(res["status_code"], 200)

                    # Both slots should now be idle again
                    self.assertFalse(runner._slots[0].is_busy)
                    self.assertFalse(runner._slots[1].is_busy)

    @patch("subprocess.run")
    def test_auto_recycle_on_recycle_recommended(self, mock_subproc):
        mock_subproc.return_value = MagicMock(returncode=0)
        runner = DockerWarmPoolRunner(
            ipc_dir=self.temp_dir,
            max_concurrent=1,
        )

        slot = runner._slots[0]
        slot.container_name = "sf-warm-worker-0-1234"
        slot.ctrl_sock_path = f"{self.temp_dir}/warm-workers/sf-warm-worker-0-1234.ctrl.sock"
        Path(slot.ctrl_sock_path).parent.mkdir(parents=True, exist_ok=True)
        Path(slot.ctrl_sock_path).write_text("dummy")

        with patch.object(runner, "_connect_to_control_socket", return_value=MagicMock()):
            with patch.object(runner, "_send_job_and_recv") as mock_send_recv:
                # Return recycle_recommended: True
                mock_send_recv.return_value = {
                    "ok": True,
                    "result": {"status_code": 200, "content": "Done"},
                    "recycle_recommended": True,
                }

                runner.run(
                    "https://example.com",
                    socket_path=f"{self.temp_dir}/job-1/relay.sock",
                    operation_id=b"0123456789abcdef",
                    deadline=time.monotonic() + 5.0,
                    max_chars=2000,
                    timeout_seconds=5.0,
                )

                # Container must be recycled (name reset to None)
                self.assertIsNone(slot.container_name)
                self.assertIsNone(slot.ctrl_sock_path)

    @patch("subprocess.run")
    def test_close_cleans_up_running_containers(self, mock_subproc):
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr="")
        runner = DockerWarmPoolRunner(
            ipc_dir=self.temp_dir,
            max_concurrent=2,
        )
        runner._slots[0].container_name = "sf-warm-worker-0-abcd"
        runner._slots[1].container_name = "sf-warm-worker-1-ef01"

        runner.close()
        self.assertTrue(runner._closed)
        self.assertIsNone(runner._slots[0].container_name)
        self.assertIsNone(runner._slots[1].container_name)

    def test_prewarm_starts_slot_0(self):
        runner = DockerWarmPoolRunner(
            ipc_dir=self.temp_dir,
            max_concurrent=2,
        )
        with patch.object(runner, "_start_container_for_slot") as mock_start:
            mock_start.return_value = "/mock/warm-workers/sf-warm-worker-0-test.ctrl.sock"
            runner.prewarm()

            mock_start.assert_called_once()
            call_slot = mock_start.call_args[0][0]
            self.assertEqual(call_slot.slot_id, 0)
            self.assertFalse(runner._slots[0].is_busy)
            self.assertFalse(runner._slots[1].is_busy)
            self.assertIsNone(runner._slots[1].container_name)

    def test_prewarm_failure_does_not_crash(self):
        runner = DockerWarmPoolRunner(
            ipc_dir=self.temp_dir,
            max_concurrent=2,
        )
        with patch.object(runner, "_start_container_for_slot", side_effect=RuntimeError("Docker connection failed")):
            # Must not raise
            runner.prewarm()
            self.assertFalse(runner._slots[0].is_busy)

    def test_daemon_prewarm_triggers_in_background_when_enabled(self):
        runner = MagicMock()
        runner.prewarm = MagicMock()

        cfg_enabled = BrowserDaemonConfig(
            worker_mode="docker_warm",
            prewarm=True,
            hmac_key="mock-key",
        )
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread

            server = create_daemon(config=cfg_enabled, runner=runner)
            mock_thread_cls.assert_called_once_with(
                target=runner.prewarm,
                daemon=True,
                name="WarmWorkerPrewarm",
            )
            mock_thread.start.assert_called_once()
            server.server_close()

    def test_daemon_prewarm_not_triggered_when_disabled(self):
        runner = MagicMock()
        runner.prewarm = MagicMock()

        cfg_disabled = BrowserDaemonConfig(
            worker_mode="docker_warm",
            prewarm=False,
            hmac_key="mock-key",
        )
        with patch("threading.Thread") as mock_thread_cls:
            server = create_daemon(config=cfg_disabled, runner=runner)
            mock_thread_cls.assert_not_called()
            runner.prewarm.assert_not_called()
            server.server_close()

    def test_config_from_env_prewarm_parsing(self):
        self.assertTrue(BrowserDaemonConfig.from_env({"BROWSER_WORKER_PREWARM": "true"}).prewarm)
        self.assertTrue(BrowserDaemonConfig.from_env({"BROWSER_WORKER_PREWARM": "1"}).prewarm)
        self.assertTrue(BrowserDaemonConfig.from_env({"BROWSER_WORKER_PREWARM": "YES"}).prewarm)
        self.assertFalse(BrowserDaemonConfig.from_env({"BROWSER_WORKER_PREWARM": "false"}).prewarm)
        self.assertFalse(BrowserDaemonConfig.from_env({}).prewarm)


if __name__ == "__main__":
    unittest.main()
