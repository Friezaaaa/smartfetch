# SmartFetch V1.2 — deployable API

SmartFetch takes a public web URL and returns clean agent-ready text, Markdown, links, metadata, and the retrieval method used. It tries cheap HTTP retrieval first and falls back to a real Chromium browser when needed.

## What changed from V1.1

- Public binding (`0.0.0.0`) for container deployment.
- Output caps: default 20,000 chars, hard max 50,000 chars per text field.
- Links capped at 50.
- Total request timeout (25s default).
- Bounded fetch/browser concurrency so one burst cannot spawn unlimited Chrome processes.
- Conservative unauthenticated rate limiter until x402 is added.
- Stronger SSRF target validation: blocks localhost, private/link-local/metadata addresses, credentials, non-HTTP(S), and nonstandard ports.
- Request IDs and structured error codes.
- `/health` and `/meta` endpoints.
- Dockerfile + Railway config.
- Remote 20-URL test that calls the deployed API instead of importing the Python function directly.

## API

### GET /health

Returns service health/version.

### GET /meta

Machine-readable service description.

### POST /fetch

```json
{
  "url": "https://example.com/article",
  "max_chars": 20000,
  "force_browser": false
}
```

`max_chars` is optional (minimum 1,000; default 20,000; maximum 50,000).

Example response fields:

```json
{
  "success": true,
  "requested_url": "https://example.com/article",
  "final_url": "https://example.com/article",
  "status_code": 200,
  "render_method": "http",
  "title": "…",
  "content": "…",
  "markdown": "…",
  "links": [],
  "word_count": 1000,
  "content_hash": "…",
  "truncated": false,
  "elapsed_ms": 350,
  "request_id": "…",
  "service_version": "1.2.0"
}
```

## Run locally

```bash
python -m pip install -r requirements.txt
python -m smartfetch.server
```

Then:

```bash
curl http://127.0.0.1:8787/health
curl -X POST http://127.0.0.1:8787/fetch \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

Windows users can run `setup_windows.bat`, then `start_windows.bat`.

## Local validation

```bash
python tests/security_smoke.py
python tests/api_local_smoke.py
```

`api_local_smoke.py` intentionally enables private-network access only inside the test process so it can use a localhost fixture.

## Container

```bash
docker build -t smartfetch:v1.2 .
docker run --rm -p 8787:8787 smartfetch:v1.2
```

The container installs Chromium automatically.

## Public deployment

This package is Docker-ready. Railway can deploy the included `Dockerfile` and `railway.json`; any Docker host that provides outbound HTTPS and enough RAM for one Chromium process should also work.

Recommended initial environment variables:

```text
MAX_CONCURRENT_BROWSERS=1
MAX_CONCURRENT_FETCHES=4
DEFAULT_MAX_OUTPUT_CHARS=20000
MAX_OUTPUT_CHARS=50000
FETCH_TIMEOUT_SECONDS=12
BROWSER_TIMEOUT_SECONDS=15
TOTAL_REQUEST_TIMEOUT_SECONDS=25
RATE_LIMIT_PER_MINUTE=30
RATE_LIMIT_BURST=10
```

Do **not** set `ALLOW_PRIVATE_NETWORK=1` in production.

### Important SSRF deployment note

Application-level URL validation is included, but browser rendering can execute page subresources. For a paid public deployment, the browser container should additionally be isolated by host/network egress policy from cloud metadata and private RFC1918 networks. We will harden this again before removing the temporary rate limit and before x402 launch.

## Test the public deployment

On Windows run `test_remote_windows.bat`, paste the public URL, and upload `tests/remote_20_results.json`.

Or:

```bash
python tests/remote_20.py https://YOUR-PUBLIC-URL
```

Our deployment gate remains **18/20 minimum**, including all five forced-browser requests.

## Next milestone

Once the remote API passes, add x402 to `/fetch`, publish payment/discovery metadata, complete the first settlement, and then test outside-agent discovery.
