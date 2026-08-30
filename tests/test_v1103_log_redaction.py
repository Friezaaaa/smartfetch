import io
import json
import logging
import socket
import threading
import time
import unittest
from urllib.parse import urlencode
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI, Request

from smartfetch import server


CANARY_QUERY = {
    'api_key': 'CANARY_API_KEY_VALUE_1103',
    'APIKEY': 'CANARY_APIKEY_VALUE_1103',
    'Key': 'CANARY_KEY_VALUE_1103',
    'TOKEN': 'CANARY_TOKEN_VALUE_1103',
    'Access_Token': 'CANARY_ACCESS_TOKEN_VALUE_1103',
    'auth': 'CANARY_AUTH_VALUE_1103',
    'AUTHORIZATION': 'CANARY_AUTHORIZATION_VALUE_1103',
    'Signature': 'CANARY_SIGNATURE_VALUE_1103',
    'SIG': 'CANARY_SIG_VALUE_1103',
    'Secret': 'CANARY_SECRET_VALUE_1103',
    'ordinary': 'query-handling-still-works',
}


class UvicornAccessLogRedactionTests(unittest.TestCase):
    def _serve_one_request(self, application, path):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        port = sock.getsockname()[1]

        config = server.create_uvicorn_config(application)
        access_logger = logging.getLogger('uvicorn.access')
        access_handlers = list(access_logger.handlers)
        streams = [getattr(handler, 'stream', None) for handler in access_handlers]
        captured = io.StringIO()
        for handler in access_handlers:
            if hasattr(handler, 'setStream'):
                handler.setStream(captured)

        instance = uvicorn.Server(config)
        thread = threading.Thread(
            target=instance.run,
            kwargs={'sockets': [sock]},
            daemon=True,
        )
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not instance.started and thread.is_alive():
                if time.monotonic() >= deadline:
                    self.fail('Uvicorn did not start within five seconds')
                time.sleep(0.01)
            with urlopen(
                f'http://127.0.0.1:{port}{path}',
                timeout=5,
            ) as response:
                status = response.status
                body = json.loads(response.read())
        finally:
            instance.should_exit = True
            thread.join(timeout=5)
            sock.close()
            for handler, stream in zip(access_handlers, streams):
                if hasattr(handler, 'setStream') and stream is not None:
                    handler.setStream(stream)

        self.assertFalse(thread.is_alive(), 'Uvicorn did not stop cleanly')
        return status, body, captured.getvalue()

    def test_query_values_are_omitted_without_mutating_the_request(self):
        application = FastAPI()

        @application.get('/mcp')
        async def mcp_probe(request: Request):
            return {'query': dict(request.query_params)}

        target = f'/mcp?{urlencode(CANARY_QUERY)}'
        status, body, access_log = self._serve_one_request(application, target)

        self.assertEqual(status, 200)
        self.assertEqual(body, {'query': CANARY_QUERY})
        self.assertIn('GET /mcp HTTP/1.1', access_log)
        self.assertIn('200 OK', access_log)
        self.assertNotIn('/mcp?', access_log)
        for name, canary in CANARY_QUERY.items():
            with self.subTest(parameter=name):
                self.assertNotIn(canary, access_log)


if __name__ == '__main__':
    unittest.main()
