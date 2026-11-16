"""Audit script: In-depth verification of VPS security layer (mTLS, HMAC, Replay, SSRF)."""

import os
import sys
import time
import secrets
import requests
from pathlib import Path

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartfetch.remote_browser import sign_request

VPS_URL = os.getenv("BROWSER_WORKER_URL", "https://159.89.40.174:8443")
CERTS_DIR = Path(__file__).resolve().parent.parent / "deploy" / "vps" / "certs"
CA_CERT = str(CERTS_DIR / "ca.crt")
CLIENT_CERT = str(CERTS_DIR / "client.crt")
CLIENT_KEY = str(CERTS_DIR / "client.key")
if "BROWSER_WORKER_HMAC_KEY" not in os.environ:
    raise RuntimeError("BROWSER_WORKER_HMAC_KEY environment variable is required")
HMAC_KEY = os.environ["BROWSER_WORKER_HMAC_KEY"]

def audit_vps_security():
    print(f"=== Auditing DigitalOcean VPS Security: {VPS_URL} ===")

    # 1. Test mTLS without client cert
    print("\n--- 1. Testing mTLS Enforcement (No Client Cert) ---")
    mtls_blocked = False
    try:
        # Connect without cert=(CLIENT_CERT, CLIENT_KEY)
        r = requests.get(f"{VPS_URL}/health", verify=CA_CERT, timeout=5)
        print(f"UNEXPECTED: Request without client cert succeeded: HTTP {r.status_code}")
    except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
        print(f"SUCCESS: Connection rejected at TLS layer without client cert: {exc}")
        mtls_blocked = True
    assert mtls_blocked, "VPS did not require mTLS client certificate!"

    # 2. Test mTLS with valid client cert
    print("\n--- 2. Testing mTLS Connection (With Client Cert) ---")
    r_health = requests.get(f"{VPS_URL}/health", cert=(CLIENT_CERT, CLIENT_KEY), verify=CA_CERT, timeout=5)
    print(f"Health status with client cert: HTTP {r_health.status_code}")
    print(f"Health body: {r_health.json()}")
    assert r_health.status_code == 200
    assert r_health.json()["ok"] is True

    # 3. Test HMAC Enforcement
    print("\n--- 3. Testing HMAC Enforcement on POST /v1/render ---")
    # 3a. Request without HMAC headers
    r_no_hmac = requests.post(
        f"{VPS_URL}/v1/render",
        json={"url": "https://example.com"},
        cert=(CLIENT_CERT, CLIENT_KEY),
        verify=CA_CERT,
        timeout=5,
    )
    print(f"Render request without HMAC: HTTP {r_no_hmac.status_code}")
    print(f"Response: {r_no_hmac.json()}")
    assert r_no_hmac.status_code == 401
    assert "hmac" in r_no_hmac.text.lower() or "signature" in r_no_hmac.text.lower()
    print("SUCCESS: Unsigned request rejected with 401 Unauthorized.")

    # 3b. Request with invalid HMAC signature
    r_bad_hmac = requests.post(
        f"{VPS_URL}/v1/render",
        json={"url": "https://example.com"},
        headers={
            "X-SmartFetch-Signature": "invalidsignature00000000000000000000000000000000000000000000000000",
            "X-SmartFetch-Timestamp": str(int(time.time())),
            "X-SmartFetch-Nonce": secrets.token_hex(16),
        },
        cert=(CLIENT_CERT, CLIENT_KEY),
        verify=CA_CERT,
        timeout=5,
    )
    print(f"Render request with bad HMAC: HTTP {r_bad_hmac.status_code}")
    assert r_bad_hmac.status_code == 401
    print("SUCCESS: Invalid HMAC signature rejected with 401 Unauthorized.")

    # 4. Test Nonce Replay Protection
    print("\n--- 4. Testing Replay Protection ---")
    fixed_nonce = secrets.token_hex(16)
    timestamp = str(int(time.time()))
    body_bytes = b'{"url":"https://example.com"}'
    
    # Generate valid signature with fixed nonce
    sig_headers = sign_request("POST", "/v1/render", body_bytes, HMAC_KEY, timestamp=timestamp, nonce=fixed_nonce)
    sig_headers["Content-Type"] = "application/json"

    # First request: should pass auth (will execute render)
    print("Sending request 1 with fresh nonce...")
    r1 = requests.post(
        f"{VPS_URL}/v1/render",
        data=body_bytes,
        headers=sig_headers,
        cert=(CLIENT_CERT, CLIENT_KEY),
        verify=CA_CERT,
        timeout=25,
    )
    print(f"Request 1 status: HTTP {r1.status_code}")
    assert r1.status_code in (200, 403, 503)

    # Second request: replay identical nonce
    print("Sending request 2 with REPLAYED nonce...")
    r2 = requests.post(
        f"{VPS_URL}/v1/render",
        data=body_bytes,
        headers=sig_headers,
        cert=(CLIENT_CERT, CLIENT_KEY),
        verify=CA_CERT,
        timeout=10,
    )
    print(f"Request 2 (replayed) status: HTTP {r2.status_code}")
    print(f"Response: {r2.json()}")
    assert r2.status_code == 401
    assert "replay" in r2.text.lower() or "nonce" in r2.text.lower()
    print("SUCCESS: Replay attack blocked with 401 Unauthorized.")

    # 5. Test SSRF Destination Policy Blocking
    print("\n--- 5. Testing SSRF Destination Policy (Metadata / Loopback / Private) ---")
    ssrf_targets = [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/",
        "http://10.0.0.1/",
        "http://[::1]/",
    ]
    for target in ssrf_targets:
        t_body = f'{{"url":"{target}"}}'.encode("utf-8")
        headers = sign_request("POST", "/v1/render", t_body, HMAC_KEY)
        headers["Content-Type"] = "application/json"
        r_ssrf = requests.post(
            f"{VPS_URL}/v1/render",
            data=t_body,
            headers=headers,
            cert=(CLIENT_CERT, CLIENT_KEY),
            verify=CA_CERT,
            timeout=10,
        )
        print(f"SSRF test for {target}: HTTP {r_ssrf.status_code} -> {r_ssrf.json().get('error_code')}")
        assert r_ssrf.status_code == 403
        assert r_ssrf.json()["error_code"] == "policy_rejection"
    print("SUCCESS: All SSRF targets blocked with 403 policy_rejection.")

    print("\n=============================================")
    print("ALL VPS SECURITY CONTROLS VERIFIED 100%!")
    print("=============================================")

if __name__ == "__main__":
    audit_vps_security()
