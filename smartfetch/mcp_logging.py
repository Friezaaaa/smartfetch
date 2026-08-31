"""Severity-correct stream routing for MCP SDK log records."""

import logging
import sys


class _BelowWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


def configure_mcp_sdk_logging() -> None:
    """Route only the MCP logger hierarchy by truthful record severity."""
    logger = logging.getLogger('mcp')
    logger.handlers.clear()

    formatter = logging.Formatter('%(levelname)s %(name)s %(message)s')

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(_BelowWarning())
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)
    logger.propagate = False
