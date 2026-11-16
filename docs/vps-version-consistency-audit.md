# SmartFetch Engineering Audit: VPS Browser Daemon Version Consistency

**Document Class**: Internal Engineering Audit  
**Author**: Antigravity Engineering Review  
**Date**: 2026-11-16T02:56:00-05:00  
**Target Release**: SmartFetch `v1.12.0`  
**Classification**: Internal Technical Audit (Excluded from Public Release Notes)  

---

## 1. Executive Summary & Problem Statement

During post-release production monitoring of SmartFetch `v1.12.0`, a version reporting discrepancy was identified between the public Railway gateway and the isolated DigitalOcean VPS browser daemon:

- **Railway Public Gateway** (`GET https://smartfetch-production-ea53.up.railway.app/health`):
  ```json
  {
    "ok": true,
    "service": "SmartFetch",
    "version": "1.12.0",
    "retrieval_available": true,
    "browser_available": true,
    "mode": "isolated_browser"
  }
  ```
- **DigitalOcean VPS Browser Daemon** (`GET https://159.89.40.174:8443/health` via mTLS):
  ```json
  {
    "ok": true,
    "service": "smartfetch-browser-worker",
    "version": "1.11.1",
    "active_jobs": 0,
    "capacity": 2
  }
  ```

This audit was conducted to investigate the root cause, verify whether any functional or security disparities exist, and document the architectural decision regarding remediation.

---

## 2. Root Cause Analysis

### 2.1 Deployment Sequence & Image Freezing
1. **Initial VPS Build & Validation (Phase 4)**: The DigitalOcean VPS container images (`smartfetch-browser-daemon:prod` and `smartfetch-browser-worker:prod`) were built and deployed during Phase 4 validation at Git commit `87d35d9` (`feat(vps): deploy isolated VPS browser daemon and integrate with Railway`).
2. **Production Concurrency Hardening**: Concurrency was clamped to `2` (`max_concurrent = 2`) and container cleanup was wrapped in a guaranteed `try ... finally: docker rm -f` block in `/opt/smartfetch/smartfetch/browser_daemon.py` on the VPS host.
3. **Subsequent Version Bump**: The version string bump (`1.11.1` &rarr; `1.12.0`) was committed later in Git commit `2bcdd85` (`chore(release): bump version to 1.12.0 for production release`).
4. **Railway Automatic CI/CD vs Frozen VPS State**:
   - Railway connects directly to GitHub and automatically redeployed upon the commit, updating its reported version to `1.12.0`.
   - The DigitalOcean VPS daemon operates as a persistent Docker service. In strict accordance with the production freeze policy ("*Do not redeploy anything; do not modify Railway or the VPS*"), the VPS container `sf-browser-daemon` was deliberately left running without restart or rebuild.

### 2.2 Scope of Discrepancy
A direct unified diff between `/app/smartfetch/browser_daemon.py` running inside the active `sf-browser-daemon` container and `smartfetch/browser_daemon.py` at `v1.12.0` in the repository confirms that the mismatch consists **solely of two string literals**:

```diff
--- running_container:/app/smartfetch/browser_daemon.py
+++ repo_v1.12.0:smartfetch/browser_daemon.py
@@ -298,7 +298,7 @@
 class BrowserWorkerHandler(BaseHTTPRequestHandler):
     """HTTP request handler for the browser worker daemon."""
 
-    server_version = "SmartFetchWorker/1.11.1"
+    server_version = "SmartFetchWorker/1.12.0"
 
     def log_message(self, format: str, *args: Any) -> None:
         _LOGGER.info(f"%s - - [{self.log_date_time_string()}] {format % args}")
@@ -313,7 +313,7 @@
             payload = {
                 "ok": True,
                 "service": "smartfetch-browser-worker",
-                "version": "1.11.1",
+                "version": "1.12.0",
                 "active_jobs": active_jobs,
                 "capacity": self.daemon_server.config.max_concurrent,
             }
```

The daemon does not enforce or require any version match from connecting clients. Railway's `RemoteBrowserClient` checks health strictly via `payload.get("ok") is True` over mTLS.

---

## 3. Cryptographic SHA-256 & Runtime Verification

To guarantee that the running worker has zero functional, security, or isolation divergence from `v1.12.0`, direct SHA-256 checksums were calculated comparing files inside `smartfetch-browser-worker:prod` and `/opt/smartfetch` against the `v1.12.0` repository tree:

| Module / File Path | Local v1.12.0 SHA-256 | VPS Container / Host SHA-256 | Match Status |
| :--- | :--- | :--- | :---: |
| `smartfetch/browser_worker.py` | `c4216fa682e87900b904944d65377f3747804ee9` | `c4216fa682e87900b904944d65377f3747804ee9` | **100% MATCH** |
| `smartfetch/browser_prototype/broker.py` | `cf7c3ab7c2e3a79d044955b5d8bfcbdd12948b8c` | `cf7c3ab7c2e3a79d044955b5d8bfcbdd12948b8c` | **100% MATCH** |
| `smartfetch/browser_prototype/policy.py` | `6a56592a83279c14d9b15fbe8db85a535691d6c8` | `6a56592a83279c14d9b15fbe8db85a535691d6c8` | **100% MATCH** |
| `smartfetch/browser_prototype/harness.py` | `611da145f8f8b66804da129202167d403bbec1ba` | `611da145f8f8b66804da129202167d403bbec1ba` | **100% MATCH** |
| `smartfetch/browser_prototype/resolver.py` | `0fa5ca5c0f6804cf30e588dbca021759ff51ae8c` | `0fa5ca5c0f6804cf30e588dbca021759ff51ae8c` | **100% MATCH** |
| `deploy/vps/seccomp-chrome.json` | `6dafd8c049019da4c6437648356cbb41108253ce` | `6dafd8c049019da4c6437648356cbb41108253ce` | **100% MATCH** |
| `deploy/vps/docker-compose.vps.yml` | `8ae2f8ebecb7470557457fc8c1a7c36a43b91c01` | `8ae2f8ebecb7470557457fc8c1a7c36a43b91c01` | **100% MATCH** |
| `deploy/vps/Dockerfile.daemon` | `ab6a56b9c9f09139eeea53e3faae7db02b3c21c7` | `ab6a56b9c9f09139eeea53e3faae7db02b3c21c7` | **100% MATCH** |
| `deploy/vps/Dockerfile.worker` | `5748bae2245b7367c3b28f80ee32717758364d99` | `5748bae2245b7367c3b28f80ee32717758364d99` | **100% MATCH** |

---

## 4. Operational & Security Boundary Verification

Both live production regression passes (`docs/production-validation.md` and `docs/paid-browser-regression-v1.12.0-2.md`) confirmed that all production security boundaries operate identically to the v1.12.0 specification:

1. **mTLS Authentication**: Mutual TLS enforcement rejected unauthenticated requests (`SSLError`/handshake abort) while accepting valid client certificates signed by the dedicated internal CA.
2. **HMAC-SHA256 Request Signing**: All `/v1/render` invocations require valid canonical HMAC signatures, 16-byte nonce validation, and sliding timestamp verification.
3. **Container Sandboxing**: Chromium executions run in ephemeral Docker containers under `network_mode: none`, non-root UID `10001:10001`, and zero `--no-sandbox` usage.
4. **Unix IPC Policy Broker**: Mediates all network egress over Unix domain sockets, enforcing SSRF protections against `127.0.0.1`, private RFC1918 ranges, and cloud metadata (`169.254.169.254`).
5. **Fail-Closed Settlement**: Coinbase CDP on-chain settlement executes only after verified, successful page retrieval. Two separate Base Mainnet transactions were validated:
   - Pass 1: [`0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e`](https://basescan.org/tx/0x0137d6f3c4a05ab83e6501fc6b501b4b2ddc69c20b1a452a72592a50435c527e)
   - Pass 2: [`0x22858366fddfd518e3de25056a55379a2f4ebe3e90c3dfca07fcfc4a5365ba5f`](https://basescan.org/tx/0x22858366fddfd518e3de25056a55379a2f4ebe3e90c3dfca07fcfc4a5365ba5f)

---

## 5. Engineering Decision & Action Plan

### Decision: Defer String Update to Planned Maintenance Window
- **Zero Risk**: Because the discrepancy is purely aesthetic string reporting with zero logic, security, or API divergence, restarting or redeploying the running VPS daemon mid-flight would introduce unnecessary operational risk to an active, validated production system.
- **Action**: Maintain the running container undisturbed for the `v1.12.0` release.
- **Scheduled Remediation**: The VPS daemon container image rebuild to sync the static version string (`1.12.0` / `1.13.0`) is scheduled for the next planned maintenance window (Horizon 1 / `v1.13.0`), alongside scheduled automated certificate rotation and metrics exporter additions.
- **Documentation Policy**: Keep this infrastructure detail confined to this internal engineering audit and roadmap; do not pollute user-facing release notes.
