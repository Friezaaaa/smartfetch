"""Headless Chrome CDP test for SmartFetch Developer Playground (examples/playground/index.html)."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import httpx
import websockets

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PLAYGROUND_URL = "http://127.0.0.1:3000/index.html"
CDP_PORT = 9444


async def run_cdp_tests():
    user_data_dir = tempfile.mkdtemp(prefix="chrome_playground_test_")
    proc = subprocess.Popen([
        CHROME_PATH,
        "--headless=new",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--disable-gpu",
        f"--user-data-dir={user_data_dir}",
        "about:blank"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        # Wait for CDP endpoint to become ready
        ws_url = None
        for _ in range(30):
            try:
                resp = httpx.get(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=1.0)
                if resp.status_code == 200 and resp.json():
                    ws_url = resp.json()[0].get("webSocketDebuggerUrl")
                    if ws_url:
                        break
            except Exception:
                pass
            await asyncio.sleep(0.3)

        assert ws_url, "Failed to connect to Chrome CDP WebSocket debugger"
        print(f"[1] Connected to Chrome CDP: {ws_url}")

        async with websockets.connect(ws_url) as ws:
            msg_id = 0
            console_errors = []
            exceptions = []

            async def send_cmd(method, params=None):
                nonlocal msg_id
                msg_id += 1
                curr_id = msg_id
                payload = {"id": curr_id, "method": method, "params": params or {}}
                await ws.send(json.dumps(payload))
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    if data.get("method") == "Runtime.exceptionThrown":
                        exceptions.append(data)
                    elif data.get("method") == "Console.messageAdded":
                        msg = data.get("params", {}).get("message", {})
                        if msg.get("level") in ("error", "warning") and "favicon" not in msg.get("text", ""):
                            console_errors.append(msg.get("text"))
                    elif data.get("id") == curr_id:
                        return data.get("result", {})

            # Enable CDP domains
            await send_cmd("Runtime.enable")
            await send_cmd("Page.enable")
            await send_cmd("Console.enable")

            # Navigate to Playground
            print(f"[2] Navigating to {PLAYGROUND_URL}...")
            nav_res = await send_cmd("Page.navigate", {"url": PLAYGROUND_URL})
            await asyncio.sleep(1.5)

            # Check for initial JS errors
            assert len(exceptions) == 0, f"Uncaught JS exceptions on load: {exceptions}"
            assert len(console_errors) == 0, f"Console errors on load: {console_errors}"
            print("[3] Zero JavaScript errors or uncaught exceptions on load! Verified clean.")

            # Helper to evaluate JS in the page
            async def eval_js(expression):
                res = await send_cmd("Runtime.evaluate", {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True
                })
                return res.get("result", {}).get("value")

            # Verify page title and header
            title = await eval_js("document.title")
            assert "SmartFetch" in title, f"Unexpected page title: {title}"
            print(f"[4] Page title: '{title}'")

            # Test 1: Click "Inspect Live 402 & Health"
            print("[5] Invoking inspectHealthAnd402()...")
            await eval_js("inspectHealthAnd402()")

            # Wait for execution status to update from Railway live response
            live_status = None
            for i in range(30):
                await asyncio.sleep(0.5)
                live_status = await eval_js("document.getElementById('executionStatus').innerHTML")
                print(f"  [poll {i}] executionStatus: {live_status}")
                if live_status and ("Gateway Online" in live_status or "Request failed" in live_status):
                    break

            print(f"[6] Received live status:\n{live_status}")
            if not live_status or "Gateway Online" not in live_status:
                print(f"DEBUG: exceptions = {exceptions}")
                print(f"DEBUG: console_errors = {console_errors}")
            assert "Gateway Online" in live_status, f"Expected Gateway Online in status, got: {live_status}"
            assert "$0.005 USDC" in live_status, f"Expected $0.005 USDC in status, got: {live_status}"
            assert "eip155:8453" in live_status, f"Expected eip155:8453 in status, got: {live_status}"
            assert "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786" in live_status, f"Expected Base payTo in status, got: {live_status}"
            assert "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp" in live_status, f"Expected Solana network in status, got: {live_status}"
            assert "88hE4EB4yZwb4F2arPXcG7efwQyU5FDN26DuVpVKVk2W" in live_status, f"Expected Solana payTo in status, got: {live_status}"
            assert "BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP" in live_status, f"Expected Solana feePayer in status, got: {live_status}"

            # Verify Markdown tab updated with 402 info
            md_text = await eval_js("document.getElementById('markdownOutput').textContent")
            print(f"[7] Markdown output snippet:\n{md_text[:200]}...")
            assert "HTTP 402 Payment Required" in md_text
            assert "0xE29Fcb6ef1534d5F138E48a8A71370BE7770A786" in md_text
            assert "88hE4EB4yZwb4F2arPXcG7efwQyU5FDN26DuVpVKVk2W" in md_text

            # Test 1b: Dual-chain switching to Solana
            print("[7b] Testing chain switch to Solana...")
            await eval_js("""(() => {
                const solRadio = document.querySelector('input[name="paymentChain"][value="solana"]');
                solRadio.checked = true;
                solRadio.dispatchEvent(new Event('change'));
            })()""")
            await asyncio.sleep(0.3)
            paid_text = await eval_js("document.getElementById('paidBtn').innerHTML")
            sol_config_display = await eval_js("document.getElementById('solanaConfigGroup').style.display")
            assert "Connect Solana Wallet" in paid_text, f"Expected Solana button text, got: {paid_text}"
            assert sol_config_display == "block", f"Expected solanaConfigGroup to be block, got: {sol_config_display}"
            print("[7c] Verified Solana radio switches button text and reveals Solana config block.")

            # Test 1c: Verify browser Solana blockhash RPC fetch
            print("[7d] Testing browser Solana blockhash RPC fetch...")
            blockhash = await eval_js("fetchRecentSolanaBlockhash()")
            assert blockhash and len(blockhash) > 20, f"Expected valid blockhash, got: {blockhash}"
            print(f"[7e] Successfully fetched live Solana blockhash from browser: {blockhash[:12]}...")

            # Test 2: SSRF client-side defense check
            print("[8] Testing SSRF client-side defense...")

            # 2a: Test 127.0.0.1
            await eval_js("""(() => {
                const input = document.getElementById('targetUrl');
                input.value = 'http://127.0.0.1/';
                input.dispatchEvent(new Event('input'));
            })()""")
            await asyncio.sleep(0.2)

            inspect_disabled = await eval_js("document.getElementById('inspectBtn').disabled")
            paid_disabled = await eval_js("document.getElementById('paidBtn').disabled")
            ssrf_warn_display = await eval_js("document.getElementById('ssrfWarning').style.display")
            status_text = await eval_js("document.getElementById('executionStatus').textContent")

            assert inspect_disabled is True, "Inspect button should be disabled for 127.0.0.1"
            assert paid_disabled is True, "Paid button should be disabled for 127.0.0.1"
            assert ssrf_warn_display == "block", "SSRF warning should be visible for 127.0.0.1"
            assert "SSRF Blocked" in status_text, f"Status should indicate SSRF Blocked, got: {status_text}"
            print("[9] Verified http://127.0.0.1/ is blocked client-side with disabled action buttons.")

            # 2b: Test 169.254.169.254
            await eval_js("""(() => {
                const input = document.getElementById('targetUrl');
                input.value = 'http://169.254.169.254/latest/meta-data/';
                input.dispatchEvent(new Event('input'));
            })()""")
            await asyncio.sleep(0.2)

            inspect_disabled = await eval_js("document.getElementById('inspectBtn').disabled")
            status_text = await eval_js("document.getElementById('executionStatus').textContent")
            assert inspect_disabled is True, "Inspect button should be disabled for 169.254.169.254"
            assert "SSRF Blocked" in status_text
            print("[10] Verified http://169.254.169.254/ is blocked client-side.")

            # 2c: Restore valid URL
            await eval_js("""(() => {
                const input = document.getElementById('targetUrl');
                input.value = 'https://quotes.toscrape.com/js/';
                input.dispatchEvent(new Event('input'));
            })()""")
            await asyncio.sleep(0.2)

            inspect_disabled = await eval_js("document.getElementById('inspectBtn').disabled")
            ssrf_warn_display = await eval_js("document.getElementById('ssrfWarning').style.display")
            assert inspect_disabled is False, "Inspect button should be re-enabled for valid URL"
            assert ssrf_warn_display == "none", "SSRF warning should be hidden for valid URL"
            print("[11] Verified valid URL re-enables buttons cleanly.")

            # Check if any exceptions occurred during all interactions
            assert len(exceptions) == 0, f"Exceptions occurred during test: {exceptions}"
            print("\n*** ALL PLAYGROUND HEADLESS CHECKS PASSED CLEANLY! ***\n")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run_cdp_tests())
