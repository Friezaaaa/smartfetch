import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ['ALLOW_PRIVATE_NETWORK'] = '1'
os.environ['BROWSER_VIRTUAL_TIME_MS'] = '600'
os.environ['CHROMIUM_HOST_RESOLVER_RULES'] = 'MAP smartfetch.test 127.0.0.1'

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartfetch.core import smart_fetch


def p(text):
    return f'<p>{text}</p>'

fixtures = [
    ('/article', 'Autonomous agents need reliable tools', f'''<!doctype html><html><head><title>Agent Article</title></head><body><nav>Home Pricing Login</nav><article><h1>Agent Article</h1>{p('Autonomous agents need reliable tools to complete tasks across the web.')}{p('This article contains enough useful prose to pass extraction without a browser fallback. Reliable retrieval saves time, tokens, and retries for machine clients.')}{p('Additional context makes this page deliberately long enough for main-content extraction to identify the article correctly.')}</article><footer>copyright</footer></body></html>''', False, None),
    ('/product', 'Industrial Widget 3000', f'''<html><head><title>Industrial Widget 3000</title></head><body><header>Store Categories Cart</header><main><h1>Industrial Widget 3000</h1>{p('A precision widget for automated assembly lines and robotics.')}{p('Price: $129.00. In stock. Ships in two business days.')}{p('Specifications include a 2.4 kg stainless steel housing and continuous-duty operating range suitable for production environments.')}</main></body></html>''', False, None),
    ('/docs', 'POST /v1/jobs', f'''<html><head><title>API Documentation</title></head><body><aside>Docs navigation</aside><main><h1>Jobs API</h1>{p('Use the Jobs API to create and track asynchronous work.')}<pre>POST /v1/jobs</pre>{p('Send a JSON request containing the job type and input payload. The API returns a job identifier and status endpoint.')}{p('Authentication uses a bearer token supplied in the Authorization header.')}</main></body></html>''', False, None),
    ('/blog', 'retrieval pipeline', f'''<html><head><title>Engineering Blog</title></head><body><main><article><h1>Building a retrieval pipeline</h1>{p('A good retrieval pipeline starts with the cheapest successful method.')}{p('Try ordinary HTTP first, then use a browser only when the page depends on client-side rendering. This keeps cost and latency under control while preserving reliability.')}{p('Normalize output so downstream agents receive one predictable schema.')}</article></main></body></html>''', False, None),
    ('/table', 'Starter', f'''<html><head><title>Pricing</title></head><body><main><h1>Plans</h1>{p('Choose the plan that matches your request volume and reliability needs.')}<table><tr><th>Plan</th><th>Price</th></tr><tr><td>Starter</td><td>$9</td></tr><tr><td>Pro</td><td>$49</td></tr></table>{p('All plans include structured JSON responses, request logs, and reasonable retry behavior.')}</main></body></html>''', False, None),
    ('/list', 'Structured JSON', f'''<html><head><title>Features</title></head><body><main><h1>Agent Fetch Features</h1>{p('The service focuses on reliable machine consumption rather than human-facing presentation.')}<ul><li>Structured JSON</li><li>Clean Markdown</li><li>Redirect handling</li><li>Browser fallback</li></ul>{p('These capabilities let autonomous clients use a consistent interface across many kinds of pages.')}</main></body></html>''', False, None),
    ('/boilerplate', 'The important paragraph', f'''<html><head><title>Noisy Site</title></head><body><div class="cookie">Accept cookies now</div><nav>{'Menu '*80}</nav><main><article><h1>Signal</h1>{p('The important paragraph explains that SmartFetch removes navigation and repeated boilerplate before returning content.')}{p('A second useful paragraph gives the extractor enough semantic content to separate the main section from noisy menus and banners.')}</article></main><footer>{'Footer '*80}</footer></body></html>''', False, None),
    ('/redirect', 'Autonomous agents need reliable tools', '', False, '/article'),
    ('/relative-links', 'Relative link test', f'''<html><head><title>Links</title></head><body><main><h1>Relative link test</h1>{p('This page validates that relative links become absolute URLs in structured output for autonomous agents.')}{p('That prevents every downstream client from having to resolve navigation references itself.')}<a href="/article">Read article</a><a href="https://example.org/absolute">Absolute</a></main></body></html>''', False, None),
    ('/entities', 'R&D', f'''<html><head><title>Entities &amp; Encoding</title></head><body><main><h1>R&amp;D Update</h1>{p('R&amp;D spending rose while the company kept prices stable &amp; expanded the engineering organization.')}{p('Correct entity decoding matters because agents should reason over human-readable text, not HTML escape sequences.')}</main></body></html>''', False, None),
    ('/unicode', 'café', f'''<html><head><meta charset="utf-8"><title>Unicode</title></head><body><main><h1>International text</h1>{p('The café serves crème brûlée while teams in 東京 and München collaborate on automation.')}{p('Unicode must survive extraction and Markdown conversion without corrupted characters or replacement symbols.')}</main></body></html>''', False, None),
    ('/malformed', 'Malformed HTML', '''<html><head><title>Broken</title><body><main><h1>Malformed HTML<p>This malformed HTML page intentionally omits closing tags. The parser should still recover useful content for the agent without failing entirely.<p>Browsers routinely repair markup like this, and a robust retrieval service needs comparable tolerance.</main>''', False, None),
    ('/long', 'paragraph number 20', '<html><head><title>Long Article</title></head><body><article><h1>Long Article</h1>'+''.join(p(f'This is paragraph number {i}. It contains meaningful explanatory content about autonomous systems, retrieval reliability, machine-readable interfaces, and cost-aware routing decisions.') for i in range(1,31))+'</article></body></html>', False, None),
    ('/main-only', 'Primary application content', f'''<html><head><title>Application</title></head><body><header>{'Header '*30}</header><main><h1>Dashboard Help</h1>{p('Primary application content describes how an operator can configure jobs, inspect execution receipts, and export machine-readable results.')}{p('The main element should be favored when article-style extraction does not apply naturally.')}</main></body></html>''', False, None),
    ('/hidden-noise', 'Visible operational content', f'''<html><head><title>Noise Test</title><style>.x{{display:none}}</style></head><body><script>{'junk'*500}</script><main><h1>Status</h1>{p('Visible operational content confirms that scripts and style blocks do not pollute the returned text.')}{p('The extraction stage should preserve meaningful human-visible information while removing executable code.')}</main></body></html>''', False, None),
    ('/js-rendered', 'Rendered inventory: 42 units', '''<!doctype html><html><head><title>Dynamic Inventory</title></head><body><main id="app"><p>Loading...</p></main><script>setTimeout(()=>{document.querySelector('#app').innerHTML='<h1>Inventory</h1><p>Rendered inventory: 42 units available for immediate shipment.</p><p>This content only appears after JavaScript executes in a real browser context, which should trigger the fallback path.</p>'},120)</script></body></html>''', True, None),
    ('/js-product', 'Dynamic price: $74', '''<!doctype html><html><head><title>Dynamic Product</title></head><body><div id="root">Loading...</div><script>setTimeout(()=>{document.querySelector('#root').innerHTML='<main><h1>Dynamic Product</h1><p>Dynamic price: $74 and current stock is 11 units.</p><p>The product information was inserted client-side and is unavailable to a basic HTTP parser before script execution.</p></main>'},150)</script></body></html>''', True, None),
    ('/news', 'Acme launched a new service', f'''<html><head><title>Acme launches service</title></head><body><nav>World Business Tech</nav><article><h1>Acme launches service</h1>{p('Acme launched a new service Tuesday designed to let autonomous software request verified business operations through a structured interface.')}{p('The company said developers can integrate the capability using standard HTTP requests and machine-readable schemas.')}{p('Industry observers expect more software to expose direct agent interfaces over the coming year.')}</article></body></html>''', False, None),
    ('/code', 'const result', f'''<html><head><title>Code Guide</title></head><body><main><h1>Quickstart</h1>{p('Call the client with a public URL and inspect the structured result.')}<pre><code>const result = await smartFetch('https://example.com');</code></pre>{p('The response contains final URL, render method, clean text, Markdown, links, timing data, and a content hash.')}</main></body></html>''', False, None),
    ('/redirect-dynamic', 'Rendered inventory: 42 units', '', True, '/js-rendered'),
]

by_path = {f[0]: f for f in fixtures}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?',1)[0]
        f = by_path.get(path)
        if not f:
            self.send_response(404); self.end_headers(); return
        _, _, html, _, redirect = f
        if redirect:
            self.send_response(302); self.send_header('Location', redirect); self.end_headers(); return
        data = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
port = server.server_address[1]

results = []
try:
    for i, (path, expected, _html, browser_expected, _redirect) in enumerate(fixtures, 1):
        host = 'smartfetch.test' if browser_expected else '127.0.0.1'
        url = f'http://{host}:{port}{path}'
        started = time.perf_counter()
        try:
            r = smart_fetch(url)
            content_ok = expected in r['content']
            render_ok = (not browser_expected) or r['render_method'] == 'browser'
            passed = bool(r['success'] and content_ok and r['markdown'] and render_ok)
            results.append({
                'n': i, 'path': path, 'pass': passed,
                'render': r['render_method'], 'words': r['word_count'],
                'chars': len(r['content']),
                'ms': round((time.perf_counter()-started)*1000),
                'note': 'ok' if passed else ('wrong render method' if content_ok and not render_ok else f'missing: {expected}')
            })
        except Exception as exc:
            results.append({'n': i,'path': path,'pass': False,'render':'error','words':0,'chars':0,'ms':round((time.perf_counter()-started)*1000),'note':str(exc)})
finally:
    server.shutdown(); server.server_close()

passed = sum(1 for r in results if r['pass'])
report = {'passed': passed, 'total': len(results), 'pass_rate': round(100*passed/len(results),1), 'results': results}
(ROOT / 'tests' / 'local_20_results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')

print(f"{'#':>2} {'PASS':<5} {'RENDER':<7} {'WORDS':>5} {'MS':>5} PATH")
for r in results:
    print(f"{r['n']:>2} {str(r['pass']):<5} {r['render']:<7} {r['words']:>5} {r['ms']:>5} {r['path']} {('- '+r['note']) if not r['pass'] else ''}")
print(f'\nResult: {passed}/{len(results)} passed ({report["pass_rate"]}%)')
raise SystemExit(0 if passed == len(results) else 1)
