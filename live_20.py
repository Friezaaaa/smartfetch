import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smartfetch.core import smart_fetch

# 15 normal SmartFetch calls + 5 forced-browser calls so the expensive fallback
# path is actually proven instead of accidentally going untested.
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
    # Browser-path verification. These are deliberately forced through Chrome.
    ('https://example.com/', True),
    ('https://developer.mozilla.org/en-US/docs/Web/HTTP', True),
    ('https://github.com/mozilla/readability', True),
    ('https://playwright.dev/docs/intro', True),
    ('https://www.nasa.gov/', True),
]

results=[]
for i,(url,force_browser) in enumerate(CASES,1):
    try:
        r=smart_fetch(url, force_browser=force_browser)
        status_ok = isinstance(r.get('status_code'), int) and 200 <= r['status_code'] < 400
        content_ok = len(r.get('content','').strip()) >= 80
        render_ok = (not force_browser) or r.get('render_method') == 'browser'
        passed = bool(r.get('success') and status_ok and content_ok and render_ok)
        results.append({
            'n':i,
            'pass':passed,
            'mode':'browser-forced' if force_browser else 'smart',
            'render':r.get('render_method'),
            'status':r.get('status_code'),
            'words':r.get('word_count',0),
            'chars':len(r.get('content','')),
            'ms':r.get('elapsed_ms'),
            'url':url,
            'final_url':r.get('final_url'),
            'reason':'ok' if passed else f'status_ok={status_ok}, content_ok={content_ok}, render_ok={render_ok}',
        })
    except Exception as exc:
        results.append({
            'n':i,'pass':False,
            'mode':'browser-forced' if force_browser else 'smart',
            'render':'error','status':None,'words':0,'chars':0,'ms':None,
            'url':url,'error':str(exc)
        })

passed=sum(1 for r in results if r['pass'])
report={'passed':passed,'total':len(results),'pass_rate':round(100*passed/len(results),1),'results':results}
(ROOT/'tests'/'live_20_results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(f"{'#':>2} {'PASS':<5} {'MODE':<14} {'RENDER':<7} {'STATUS':>6} {'WORDS':>5} {'MS':>6} URL")
for r in results:
    print(f"{r['n']:>2} {str(r['pass']):<5} {r['mode']:<14} {r['render']:<7} {str(r['status']):>6} {r['words']:>5} {str(r['ms']):>6} {r['url']}")
print(f'\nLive result: {passed}/{len(results)} passed ({report["pass_rate"]}%)')
