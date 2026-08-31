import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANARIES = {
    'debug': 'V1105_MCP_DEBUG_CANARY',
    'info': 'V1105_MCP_INFO_CANARY',
    'warning': 'V1105_MCP_WARNING_CANARY',
    'error': 'V1105_MCP_ERROR_CANARY',
    'critical': 'V1105_MCP_CRITICAL_CANARY',
}


class MCPLogSeverityTests(unittest.TestCase):
    def _run_logging_probe(self):
        script = textwrap.dedent(
            f"""
            import logging

            from smartfetch.mcp_server import create_smartfetch_mcp
            from smartfetch.payments import X402Settings

            async def unused_fetch(url, force_browser, max_chars):
                raise AssertionError('retrieval must not run')

            create_smartfetch_mcp(
                X402Settings(False, None, '$0.005', 'eip155:84532'),
                unused_fetch,
            )
            create_smartfetch_mcp(
                X402Settings(False, None, '$0.005', 'eip155:84532'),
                unused_fetch,
            )
            logging.getLogger('mcp').setLevel(logging.DEBUG)
            logger = logging.getLogger('mcp.server.lowlevel.server')
            logger.debug('{CANARIES['debug']}')
            logger.info('{CANARIES['info']}')
            logger.warning('{CANARIES['warning']}')
            logger.error('{CANARIES['error']}')
            logger.critical('{CANARIES['critical']}')
            try:
                raise RuntimeError('V1105_MCP_TRACEBACK_CANARY')
            except RuntimeError:
                logger.exception('V1105_MCP_EXCEPTION_CANARY')
            """
        )
        env = dict(os.environ)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['X402_ENABLED'] = 'false'
        return subprocess.run(
            [sys.executable, '-B', '-c', script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_sdk_debug_and_info_use_stdout(self):
        result = self._run_logging_probe()

        self.assertEqual(result.returncode, 0, result.stderr)
        for level in ('debug', 'info'):
            with self.subTest(level=level):
                canary = CANARIES[level]
                self.assertIn(canary, result.stdout)
                self.assertNotIn(canary, result.stderr)

    def test_sdk_warning_error_and_critical_use_stderr_without_duplication(self):
        result = self._run_logging_probe()

        self.assertEqual(result.returncode, 0, result.stderr)
        for level in ('warning', 'error', 'critical'):
            with self.subTest(level=level):
                canary = CANARIES[level]
                self.assertNotIn(canary, result.stdout)
                self.assertIn(canary, result.stderr)

        combined = result.stdout + result.stderr
        for canary in CANARIES.values():
            with self.subTest(canary=canary):
                self.assertEqual(combined.count(canary), 1)
        self.assertNotIn('V1105_MCP_EXCEPTION_CANARY', result.stdout)
        self.assertEqual(result.stderr.count('V1105_MCP_EXCEPTION_CANARY'), 1)
        self.assertEqual(result.stderr.count('V1105_MCP_TRACEBACK_CANARY'), 1)
        self.assertIn('Traceback (most recent call last)', result.stderr)


if __name__ == '__main__':
    unittest.main()
