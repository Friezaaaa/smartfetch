import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if len(sys.argv) < 2:
    raise SystemExit('Usage: python tests/remote_20.py https://YOUR-PUBLIC-URL')
BASE=sys.argv[1].rstrip('/')
ROOT=Path(__file__).resolve().parents[1]
CASES = [
    ('https://example.com/', False),
    ('https://www.iana.org/help/example-domains', False),
    ('https://developer.mozilla.org/en-US/docs/Web/HTTP', False),
    ('https://nodejs.org/en/learn/getting-started/introduction-to-nodejs', False),
    ('https://www.python.org/about/', False),
    ('https://www.gnu.org/philosophy/free-sw.html', False),
    ('https://en.wikipedia.org/wiki/Web_scraping', False),
    ('https://github.com/mozilla/readability', False),
    ('https://playwright.dev/docs/intro', False),
    ('https://www.sqlite.org/about.html', False),
    ('https://www.postgresql.org/about/', False),
    ('https://www.cloudflare.com/learning/bots/what-is-a-bot/', False),
    ('https://www.nasa.gov/', False),
    ('https://www.loc.gov/', False),
    ('https://www.usa.gov/', False),
    ('https://example.com/', True),
    ('https://developer.mozilla.org/en-US/docs/Web/HTTP', True),
    ('https://github.com/mozilla/readability', True),
    ('https://playwright.dev/docs/intro', True),
    ('https://www.nasa.gov/', True),
]
results=[]
for i,(url,force) in enumerate(CASES,1):
    started=time.perf_counter()
    body=json.dumps({'url':url,'force_browser':force,'max_chars':20000}).encode()
    req=Request(BASE+'/fetch',data=body,headers={'Content-Type':'application/json'},method='POST')
    try:
        with urlopen(req,timeout=35) as r: out=json.load(r)
        status_ok=200 <= int(out.get('status_code',0)) < 400
        content_ok=len(out.get('content','').strip()) >= 80 or url=='https://example.com/'
        render_ok=(not force) or out.get('render_method')=='browser'
        passed=bool(out.get('success') and status_ok and content_ok and render_ok)
        result={'n':i,'pass':passed,'mode':'browser-forced' if force else 'smart','render':out.get('render_method'),'status':out.get('status_code'),'chars':len(out.get('content','')),'ms':out.get('elapsed_ms'),'round_trip_ms':round((time.perf_counter()-started)*1000),'url':url,'reason':'ok' if passed else f'status_ok={status_ok}, content_ok={content_ok}, render_ok={render_ok}'}
    except Exception as e:
        result={'n':i,'pass':False,'mode':'browser-forced' if force else 'smart','render':'error','status':None,'chars':0,'ms':None,'round_trip_ms':round((time.perf_counter()-started)*1000),'url':url,'error':str(e)}
    results.append(result); print(f"{i:>2} {'PASS' if result['pass'] else 'FAIL'} {result['mode']:<14} {result['url']}")
passed=sum(r['pass'] for r in results)
report={'base_url':BASE,'passed':passed,'total':len(results),'pass_rate':round(passed*100/len(results),1),'results':results}
path=ROOT/'tests'/'remote_20_results.json'; path.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(f'\nRemote result: {passed}/{len(results)} ({report["pass_rate"]}%)')
print('Saved:',path)
