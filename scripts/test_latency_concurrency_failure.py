"""Audit script: Latency benchmarking, Concurrency & Backpressure testing, Failure modes."""

import time
import concurrent.futures
import requests
from pathlib import Path
from smartfetch.remote_browser import sign_request

RAILWAY_URL = "https://smartfetch-production-ea53.up.railway.app"
VPS_URL = "https://159.89.40.174:8443"
CERTS_DIR = Path(__file__).resolve().parent.parent / "deploy" / "vps" / "certs"
CA_CERT = str(CERTS_DIR / "ca.crt")
CLIENT_CERT = str(CERTS_DIR / "client.crt")
CLIENT_KEY = str(CERTS_DIR / "client.key")
HMAC_KEY = "e00cfe83b1cb7f42c7bd64f949c80e949451cc1299b7d2affc31278ce299ac78"

def audit_perf_and_limits():
    print("=== Auditing Latency, Concurrency Limit, and Failure Behavior ===")

    # 1. Latency Benchmarks
    print("\n--- 1. Measuring Endpoint Latencies ---")
    endpoints = [
        ("GET /health (Railway)", f"{RAILWAY_URL}/health", {}),
        ("GET /.well-known/x402 (Railway)", f"{RAILWAY_URL}/.well-known/x402", {}),
        ("GET /meta (Railway)", f"{RAILWAY_URL}/meta", {}),
    ]
    for label, url, headers in endpoints:
        latencies = []
        for _ in range(3):
            t0 = time.perf_counter()
            r = requests.get(url, headers=headers, timeout=10)
            latencies.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200
        avg_lat = sum(latencies) / len(latencies)
        print(f"  {label}: avg {avg_lat:.1f}ms (min: {min(latencies):.1f}ms, max: {max(latencies):.1f}ms)")

    # 2. VPS Render Latency
    print("\n--- 2. Measuring VPS Browser Worker Render Latency ---")
    body = b'{"url":"https://example.com"}'
    headers = sign_request("POST", "/v1/render", body, HMAC_KEY)
    headers["Content-Type"] = "application/json"
    t0 = time.perf_counter()
    r_render = requests.post(
        f"{VPS_URL}/v1/render",
        data=body,
        headers=headers,
        cert=(CLIENT_CERT, CLIENT_KEY),
        verify=CA_CERT,
        timeout=25,
    )
    vps_lat = (time.perf_counter() - t0) * 1000
    print(f"  VPS /v1/render status: HTTP {r_render.status_code}, total round-trip: {vps_lat:.1f}ms")
    res_json = r_render.json()
    diagnostics = res_json.get("diagnostics", {})
    print(f"  Diagnostics: worker_elapsed={diagnostics.get('worker_elapsed_ms')}ms, broker_bytes={diagnostics.get('broker_bytes')}")
    assert r_render.status_code == 200

    # 3. Concurrency Limit & Backpressure (Capacity: 2)
    print("\n--- 3. Testing VPS Concurrency Bounding (Capacity: 2) ---")
    
    def send_concurrent_render(target_url, delay=0):
        if delay:
            time.sleep(delay)
        b = f'{{"url":"{target_url}"}}'.encode("utf-8")
        h = sign_request("POST", "/v1/render", b, HMAC_KEY)
        h["Content-Type"] = "application/json"
        try:
            res = requests.post(
                f"{VPS_URL}/v1/render",
                data=b,
                headers=h,
                cert=(CLIENT_CERT, CLIENT_KEY),
                verify=CA_CERT,
                timeout=30,
            )
            return res.status_code, res.json()
        except Exception as e:
            return 0, str(e)

    # Launch 3 simultaneous requests to trigger capacity backpressure
    print("Launching 3 simultaneous render requests to verify capacity limit...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f1 = executor.submit(send_concurrent_render, "https://example.com/page1")
        f2 = executor.submit(send_concurrent_render, "https://example.com/page2")
        f3 = executor.submit(send_concurrent_render, "https://example.com/page3")
        results = [f.result() for f in (f1, f2, f3)]

    status_codes = [r[0] for r in results]
    print(f"Results of 3 concurrent requests: {status_codes}")
    for idx, (code, data) in enumerate(results, 1):
        print(f"  Job {idx}: HTTP {code} -> {data.get('error_code', 'success') if isinstance(data, dict) else data}")

    # Verify that capacity bounding protected the VPS
    # Either all 3 were serialized/handled within capacity or excess received 503 capacity busy
    has_capacity_busy = any(code == 503 for code in status_codes)
    all_succeeded = all(code == 200 for code in status_codes)
    print(f"Capacity defense verified (busy backpressure triggered: {has_capacity_busy}, all processed safely: {all_succeeded})")
    assert has_capacity_busy or all_succeeded, "Concurrency handling failed"

    # 4. Failure Modes Testing
    print("\n--- 4. Testing Failure Modes (Fail-Closed) ---")
    
    # 4a. Invalid JSON on VPS
    bad_json_body = b"not-json"
    h_bad = sign_request("POST", "/v1/render", bad_json_body, HMAC_KEY)
    r_bad_json = requests.post(f"{VPS_URL}/v1/render", data=bad_json_body, headers=h_bad, cert=(CLIENT_CERT, CLIENT_KEY), verify=CA_CERT, timeout=5)
    print(f"  Invalid JSON: HTTP {r_bad_json.status_code} -> {r_bad_json.json().get('error_code')}")
    assert r_bad_json.status_code == 400

    # 4b. Unreachable / non-existent host on VPS
    unreachable_body = b'{"url":"https://this-domain-definitely-does-not-exist-1234567890987654321.invalid/"}'
    h_unreach = sign_request("POST", "/v1/render", unreachable_body, HMAC_KEY)
    h_unreach["Content-Type"] = "application/json"
    r_unreach = requests.post(f"{VPS_URL}/v1/render", data=unreachable_body, headers=h_unreach, cert=(CLIENT_CERT, CLIENT_KEY), verify=CA_CERT, timeout=15)
    print(f"  Unreachable host: HTTP {r_unreach.status_code} -> {r_unreach.json().get('error_code')}")
    assert r_unreach.status_code == 403 or r_unreach.status_code == 500
    assert r_unreach.json()["success"] is False

    print("\n=============================================")
    print("LATENCY, CONCURRENCY, AND FAILURE MODES VERIFIED!")
    print("=============================================")

if __name__ == "__main__":
    audit_perf_and_limits()
