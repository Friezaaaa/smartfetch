import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import threading
from urllib.parse import quote

import requests
import websocket

from .security import validate_public_url
from .config import MAX_CONCURRENT_BROWSERS

_BROWSER_SLOTS = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_BROWSERS))


def _chromium_path() -> str:
    explicit = os.getenv('CHROMIUM_PATH')
    if explicit and os.path.exists(explicit):
        return explicit
    for name in ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'msedge'):
        path = shutil.which(name)
        if path:
            return path

    candidates = []
    for env_name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        base = os.getenv(env_name)
        if base:
            candidates.extend([
                os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'),
                os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            ])
    for path in candidates:
        if os.path.exists(path):
            return path

    raise RuntimeError('Chromium/Chrome/Edge not found. Install Chrome or set CHROMIUM_PATH.')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _cdp_call(ws, message_id: int, method: str, params=None, timeout=5):
    ws.send(json.dumps({'id': message_id, 'method': method, 'params': params or {}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get('id') == message_id:
            if 'error' in msg:
                raise RuntimeError(f'CDP {method} failed: {msg["error"]}')
            return msg.get('result', {})
    raise TimeoutError(f'CDP {method} timed out')


def _stop_process_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(
                ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _cleanup_profile(profile):
    # Chrome on Windows can hold cache-journal files briefly after the parent exits.
    # Cleanup must never turn an otherwise successful fetch into a failed request.
    for _ in range(8):
        try:
            shutil.rmtree(profile)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.2)
        except OSError:
            time.sleep(0.2)
    shutil.rmtree(profile, ignore_errors=True)


def browser_fetch(url: str) -> dict:
    safe = validate_public_url(url)
    if not _BROWSER_SLOTS.acquire(timeout=2):
        raise RuntimeError('Browser capacity is busy; retry shortly')
    try:
        return _browser_fetch_locked(safe)
    finally:
        _BROWSER_SLOTS.release()


def _browser_fetch_locked(safe: str) -> dict:
    chromium = _chromium_path()
    xvfb = shutil.which('xvfb-run')
    port = _free_port()
    timeout = float(os.getenv('BROWSER_TIMEOUT_SECONDS', '15'))
    settle = float(os.getenv('BROWSER_SETTLE_SECONDS', '0.8'))
    profile = tempfile.mkdtemp(prefix='smartfetch-chrome-')

    chrome_args = [
        chromium,
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',
        '--disable-background-networking',
        '--no-proxy-server',
        '--disable-component-update',
        '--disable-extensions',
        '--disable-default-apps',
        '--disable-popup-blocking',
        '--disable-notifications',
        '--disable-sync',
        '--no-first-run',
        '--no-default-browser-check',
        '--remote-allow-origins=*',
        *([f"--host-resolver-rules={os.getenv('CHROMIUM_HOST_RESOLVER_RULES')}"] if os.getenv('CHROMIUM_HOST_RESOLVER_RULES') else []),
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile}',
        'about:blank',
    ]
    cmd = ([xvfb, '-a'] + chrome_args) if xvfb else ([chromium, '--headless=new'] + chrome_args[1:])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name != 'nt'),
    )

    session = requests.Session()
    session.trust_env = False
    base = f'http://127.0.0.1:{port}'
    deadline = time.time() + min(timeout, 8)
    ws = None
    try:
        version = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('Chromium exited before DevTools became ready')
            try:
                r = session.get(base + '/json/version', timeout=0.5)
                if r.ok:
                    version = r.json()
                    break
            except requests.RequestException:
                pass
            time.sleep(0.1)
        if not version:
            raise RuntimeError('Timed out starting Chromium DevTools')

        create = session.put(base + '/json/new?' + quote(safe, safe=':/?&=%'), timeout=2)
        create.raise_for_status()
        target = create.json()
        ws_url = target['webSocketDebuggerUrl']
        ws = websocket.create_connection(ws_url, timeout=timeout, origin=base)

        _cdp_call(ws, 1, 'Page.enable')
        _cdp_call(ws, 2, 'Runtime.enable')
        nav = _cdp_call(ws, 3, 'Page.navigate', {'url': safe}, timeout=timeout)
        if nav.get('errorText'):
            raise RuntimeError(f'Navigation failed: {nav["errorText"]}')

        end = time.time() + timeout
        call_id = 4
        while time.time() < end:
            time.sleep(0.15)
            try:
                ready_result = _cdp_call(ws, call_id, 'Runtime.evaluate', {
                    'expression': 'document.readyState',
                    'returnByValue': True,
                }, timeout=2)
                call_id += 1
                ready = ready_result.get('result', {}).get('value')
                if ready in {'interactive', 'complete'}:
                    break
            except Exception:
                call_id += 1
        time.sleep(settle)

        html_result = _cdp_call(ws, call_id, 'Runtime.evaluate', {
            'expression': 'document.documentElement.outerHTML',
            'returnByValue': True,
        }, timeout=5)
        call_id += 1
        url_result = _cdp_call(ws, call_id, 'Runtime.evaluate', {
            'expression': 'location.href',
            'returnByValue': True,
        }, timeout=5)
        call_id += 1
        status_result = _cdp_call(ws, call_id, 'Runtime.evaluate', {
            'expression': '(performance.getEntriesByType("navigation")[0] && performance.getEntriesByType("navigation")[0].responseStatus) || 200',
            'returnByValue': True,
        }, timeout=5)

        html = html_result.get('result', {}).get('value', '')
        final_url = url_result.get('result', {}).get('value', safe)
        raw_status = status_result.get('result', {}).get('value', 200)
        try:
            status_code = int(raw_status or 200)
        except (TypeError, ValueError):
            status_code = 200

        validate_public_url(final_url)
        if status_code >= 400:
            raise RuntimeError(f'Browser navigation returned HTTP {status_code}')
        if not html.strip():
            raise RuntimeError('Browser rendered an empty document')

        return {
            'html': html,
            'final_url': final_url,
            'status_code': status_code,
            'content_type': 'text/html',
        }
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        _stop_process_tree(proc)
        _cleanup_profile(profile)
