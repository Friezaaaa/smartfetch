"""Uvicorn access-log configuration that never records query strings."""

from copy import deepcopy
import logging

from uvicorn.config import LOGGING_CONFIG


class QueryStringAccessFilter(logging.Filter):
    """Remove the query component from Uvicorn's formatted request target."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            request_target = args[2]
            if isinstance(request_target, str):
                sanitized_args = list(args)
                sanitized_args[2] = request_target.partition('?')[0]
                record.args = tuple(sanitized_args)
        return True


def uvicorn_log_config() -> dict:
    """Return Uvicorn's standard logging config with safe access records."""
    config = deepcopy(LOGGING_CONFIG)
    config['filters'] = {
        **config.get('filters', {}),
        'omit_query_string': {'()': QueryStringAccessFilter},
    }
    access_handler = config['handlers']['access']
    access_handler['filters'] = [
        *access_handler.get('filters', []),
        'omit_query_string',
    ]
    return config
