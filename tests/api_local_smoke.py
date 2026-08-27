import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PORT_FIXTURE = 18991
PORT_API = 18992

class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
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
    deadline=time.time()+8
    while time.time()<deadline:
        try:
            with urlopen(f'http://127.0.0.1:{PORT_API}/health', timeout=0.5) as r:
                if r.status==200: break
        except Exception: time.sleep(0.1)
    else: raise RuntimeError('API did not start')

    with urlopen(f'http://127.0.0.1:{PORT_API}/meta', timeout=2) as r:
        meta=json.load(r)
    assert meta['payment']=='not-enabled-yet'
    assert meta['mcp']['tools']==[
        'fetch_webpage',
        'webpage_to_markdown',
        'extract_webpage_text',
        'render_webpage',
    ]

    for discovery_path in ('/docs','/openapi.json','/llms.txt','/robots.txt','/sitemap.xml'):
        with urlopen(f'http://127.0.0.1:{PORT_API}{discovery_path}', timeout=2) as r:
            assert r.status==200

    payload=json.dumps({'url':f'http://127.0.0.1:{PORT_FIXTURE}/redirect','max_chars':1000}).encode()
    req=Request(f'http://127.0.0.1:{PORT_API}/fetch',data=payload,headers={'Content-Type':'application/json'},method='POST')
    with urlopen(req, timeout=10) as r:
        out=json.load(r)
    assert out['success'] is True
    assert out['status_code']==200
    assert out['render_method']=='http'
    assert 'SmartFetch local API test' in out['content']
    assert out['service_version']=='1.9.0'
    assert out['max_chars']==1000
    assert out['request_id']
    print('PASS /health')
    print('PASS /meta free mode')
    print('PASS discovery routes free')
    print('PASS /fetch end-to-end')
    print(json.dumps({k:out[k] for k in ['status_code','render_method','elapsed_ms','word_count','truncated','request_id']},indent=2))
finally:
    proc.terminate()
    try: proc.wait(timeout=3)
    except subprocess.TimeoutExpired: proc.kill()
    fixture.shutdown()
