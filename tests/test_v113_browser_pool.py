"""Tests for v1.13 Phase 3: Browser Worker Pooling."""

import time
import unittest
from unittest.mock import MagicMock, patch

from smartfetch.browser_daemon import (
    BrowserDaemonConfig,
    WarmPoolWorkerRunner,
    create_daemon,
)
from smartfetch.browser_pool import (
    BrowserPoolManager,
    PoolExhaustedError,
    WarmChromiumInstance,
    get_browser_pool,
    get_process_rss,
)


class TestWarmChromiumInstance(unittest.TestCase):
    """Verifies WarmChromiumInstance lifecycle, health checks, and context disposal."""

    def test_health_checks(self):
        inst = WarmChromiumInstance(
            instance_id="test-1",
            max_jobs=2,
            max_age=1.0,
            max_rss=1000000,
        )
        # Not healthy without process
        self.assertFalse(inst.is_healthy())

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 1234
        inst.proc = mock_proc

        # Healthy initially
        self.assertTrue(inst.is_healthy())

        # Unhealthy when process terminates
        mock_proc.poll.return_value = 1
        self.assertFalse(inst.is_healthy())
        mock_proc.poll.return_value = None

        # Unhealthy when jobs_executed >= max_jobs
        inst.metrics.jobs_executed = 2
        self.assertFalse(inst.is_healthy())
        inst.metrics.jobs_executed = 0

        # Unhealthy when age exceeds max_age
        inst.metrics.created_at = time.monotonic() - 2.0
        self.assertFalse(inst.is_healthy())

    @patch("smartfetch.browser_pool.websocket.create_connection")
    @patch("smartfetch.browser_pool.extract_content")
    def test_render_creates_and_disposes_browser_context(self, mock_extract, mock_ws_connect):
        inst = WarmChromiumInstance(instance_id="test-render")
        inst.ws_endpoint = "ws://127.0.0.1:9222/devtools/browser/abc"

        mock_ws = MagicMock()
        mock_ws_connect.return_value = mock_ws

        # Mock CDP message exchanges
        responses = [
            # 1. Target.createBrowserContext
            '{"id": 1, "result": {"browserContextId": "ctx-123"}}',
            # 2. Target.createTarget
            '{"id": 2, "result": {"targetId": "target-456"}}',
            # 3. Target.attachToTarget
            '{"id": 3, "result": {"sessionId": "sess-789"}}',
            # 4. Page.enable
            '{"id": 4, "result": {}}',
            # 5. Page.navigate
            '{"id": 5, "result": {}}',
            # 6. Runtime.evaluate (HTML)
            '{"id": 6, "result": {"result": {"value": "<html><body>Hello</body></html>"}}}',
            # 7. Runtime.evaluate (URL)
            '{"id": 7, "result": {"result": {"value": "https://example.com/page"}}}',
            # 8. Target.disposeBrowserContext
            '{"id": 8, "result": {}}',
        ]
        mock_ws.recv.side_effect = responses
        mock_extract.return_value = {
            "title": "Hello",
            "content": "Hello",
            "markdown": "Hello",
            "links": [],
            "low_quality": False,
        }

        res = inst.render(
            "https://example.com/page",
            deadline=time.monotonic() + 5.0,
            max_chars=1000,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["render_method"], "browser")
        self.assertEqual(inst.metrics.jobs_executed, 1)

        # Confirm Target.disposeBrowserContext was sent
        sent_payloads = [call[0][0] for call in mock_ws.send.call_args_list]
        disposed = any("Target.disposeBrowserContext" in p and "ctx-123" in p for p in sent_payloads)
        self.assertTrue(disposed, "BrowserContext was not properly disposed!")

    @patch("smartfetch.browser_pool.websocket.create_connection")
    def test_render_failure_marks_instance_tainted(self, mock_ws_connect):
        inst = WarmChromiumInstance(instance_id="test-fail")
        inst.ws_endpoint = "ws://127.0.0.1:9222/devtools/browser/abc"

        mock_ws = MagicMock()
        mock_ws.recv.side_effect = RuntimeError("DevTools connection crashed")
        mock_ws_connect.return_value = mock_ws

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        inst.proc = mock_proc

        self.assertTrue(inst.is_healthy())
        with self.assertRaises(RuntimeError):
            inst.render("https://example.com/fail", deadline=time.monotonic() + 1.0)

        # Instance must now be marked tainted and unhealthy
        self.assertTrue(inst._tainted)
        self.assertFalse(inst.is_healthy())


class TestBrowserPoolManager(unittest.TestCase):
    """Verifies BrowserPoolManager acquisition, recycling, and status metrics."""

    def test_pool_manager_acquire_and_release(self):
        pool = BrowserPoolManager(pool_size=1)
        mock_inst = MagicMock(spec=WarmChromiumInstance)
        mock_inst.instance_id = "inst-mock"
        mock_inst.is_busy = False
        mock_inst.is_healthy.return_value = True
        mock_inst.metrics.jobs_executed = 0
        mock_inst.proc = MagicMock()
        mock_inst.proc.pid = 9999

        pool._instances.append(mock_inst)

        acquired = pool.acquire()
        self.assertIs(acquired, mock_inst)
        self.assertTrue(mock_inst.is_busy)

        pool.release(acquired)
        self.assertFalse(mock_inst.is_busy)

    def test_pool_manager_recycles_unhealthy_instance(self):
        pool = BrowserPoolManager(pool_size=1)
        mock_inst = MagicMock(spec=WarmChromiumInstance)
        mock_inst.instance_id = "inst-retire"
        mock_inst.is_busy = False
        mock_inst.is_healthy.return_value = False  # Triggers retirement

        pool._instances.append(mock_inst)

        with patch.object(WarmChromiumInstance, "start"):
            acquired = pool.acquire()
            self.assertIsNot(acquired, mock_inst)
            mock_inst.close.assert_called_once()
            self.assertEqual(pool._recycle_counter, 1)

    def test_get_status_metrics(self):
        pool = BrowserPoolManager(pool_size=2)
        status = pool.get_status()
        self.assertEqual(status["pool_size_target"], 2)
        self.assertEqual(status["instances_alive"], 0)
        self.assertEqual(status["instances_busy"], 0)

    def test_pool_manager_enforces_hard_capacity_limit(self):
        pool = BrowserPoolManager(pool_size=1)
        mock_inst = MagicMock(spec=WarmChromiumInstance)
        mock_inst.instance_id = "inst-busy"
        mock_inst.is_busy = True
        mock_inst.is_healthy.return_value = True
        pool._instances.append(mock_inst)

        # Capacity is 1 and 1 instance is busy. Must not spawn new instances!
        with self.assertRaises(PoolExhaustedError):
            pool.acquire(timeout=0.05)

        self.assertEqual(len(pool._instances), 1)

    def test_pool_manager_waiting_and_release_wakes_waiting_thread(self):
        import threading
        pool = BrowserPoolManager(pool_size=1)
        mock_inst = MagicMock(spec=WarmChromiumInstance)
        mock_inst.instance_id = "inst-1"
        mock_inst.is_busy = True
        mock_inst.is_healthy.return_value = True
        pool._instances.append(mock_inst)

        # Schedule release after 50ms
        def delayed_release():
            time.sleep(0.05)
            pool.release(mock_inst)

        t = threading.Thread(target=delayed_release)
        t.start()

        # acquire() should wait and succeed once released
        acquired = pool.acquire(timeout=2.0)
        t.join()

        self.assertIs(acquired, mock_inst)
        self.assertTrue(mock_inst.is_busy)


class TestDaemonPoolIntegration(unittest.TestCase):
    """Verifies daemon integration with WarmPoolWorkerRunner."""

    def test_create_daemon_selects_warm_pool_runner(self):
        cfg = BrowserDaemonConfig(worker_mode="warm_pool")
        daemon = create_daemon(cfg)
        self.assertIsInstance(daemon.runner, WarmPoolWorkerRunner)


if __name__ == "__main__":
    unittest.main()
