import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PORT_FIXTURE = 18991
PORT_API = 18992

class Fixture(BaseHTTPRequestHandler):
    hits = 0
    def do_GET(self):
        Fixture.hits += 1
        if self.path == '/redirect':
            self.send_response(302); self.send_header('Location','/article'); self.end_headers(); return
        body = b'''<!doctype html><html><head><title>Local Test</title></head><body><nav>noise</nav><main><h1>SmartFetch local API test</h1><p>This is useful article content for validating the production API layer. It contains enough text to pass extraction and verifies that navigation boilerplate is removed correctly from the result.</p><p>Second paragraph with a <a href="/next">useful link</a>.</p></main></body></html>'''
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass

fixture = ThreadingHTTPServer(('127.0.0.1', PORT_FIXTURE), Fixture)
threading.Thread(target=fixture.serve_forever, daemon=True).start()

env = dict(os.environ)
env.update({'ALLOW_PRIVATE_NETWORK':'1','HOST':'127.0.0.1','PORT':str(PORT_API),'DEFAULT_MAX_OUTPUT_CHARS':'1000','X402_ENABLED':'false'})
proc = subprocess.Popen([sys.executable,'-m','smartfetch.server'], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    deadline=time.time()+30
    while time.time()<deadline:
        try:
            with urlopen(f'http://127.0.0.1:{PORT_API}/health', timeout=0.5) as r:
                if r.status==200: break
        except Exception: time.sleep(0.1)
    else: raise RuntimeError('API did not start')

    with urlopen(f'http://127.0.0.1:{PORT_API}/meta', timeout=2) as r:
        meta=json.load(r)
    assert meta['version']=='1.11.1'
    assert meta['retrieval_available'] is False
    assert meta['browser_available'] is False
    assert meta['fetch']['availability']=='unavailable'
    assert meta['fetch']['payment_challenge_enabled'] is False
    assert [tool['name'] for tool in meta['tools']]==[
        'fetch_webpage',
        'webpage_to_markdown',
        'extract_webpage_text',
        'render_webpage',
    ]

    for discovery_path in ('/','/docs','/openapi.json','/llms.txt','/robots.txt','/sitemap.xml','/.well-known/x402'):
        with urlopen(f'http://127.0.0.1:{PORT_API}{discovery_path}', timeout=2) as r:
            assert r.status==200

    payload=json.dumps({'url':f'http://127.0.0.1:{PORT_FIXTURE}/redirect','max_chars':1000}).encode()
    req=Request(f'http://127.0.0.1:{PORT_API}/fetch',data=payload,headers={'Content-Type':'application/json'},method='POST')
    try:
        urlopen(req, timeout=10)
        raise AssertionError('retrieval unexpectedly succeeded')
    except HTTPError as error:
        assert error.code==503
        assert error.headers['Retry-After']=='3600'
        assert error.headers['Cache-Control']=='no-store'
        assert 'payment-required' not in error.headers
        out=json.load(error)
    assert out['error_code']=='retrieval_unavailable'
    assert Fixture.hits==0
    print('PASS /health')
    print('PASS /meta emergency mode')
    print('PASS discovery routes free')
    print('PASS /fetch unavailable before retrieval or payment')
finally:
    proc.terminate()
    try: proc.wait(timeout=3)
    except subprocess.TimeoutExpired: proc.kill()
    fixture.shutdown()
