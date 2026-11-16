"""Disposable, synthetic-only two-container Linux browser boundary experiment.

Broker mode has a networked synthetic fixture. Worker mode must be launched
separately with Docker --network none and the same IPC volume. Neither mode is
imported by production or contacts an external destination.
"""

import json
import ipaddress
import os
from pathlib import Path
import http.client
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


package = types.ModuleType("smartfetch")
package.__path__ = ["/app/smartfetch"]
sys.modules["smartfetch"] = package

from smartfetch.browser_prototype.broker import Broker
from smartfetch.browser_prototype.policy import DestinationPolicy, PolicyError
from smartfetch.browser_prototype.relay import BridgeServer, WorkerRelay
from smartfetch.browser_prototype.worker import (WorkerBoundaryError,
                                                 inspect_boundary,
                                                 render_in_isolated_worker)


ROOT = Path("/ipc")
OPERATION_ID = b"local-test-01234"  # fixed synthetic ID; not a credential


def _write_bounded(path: Path, value: dict) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 4096:
        raise RuntimeError("gate_output_too_large")
    path.write_text(encoded, encoding="ascii")


def run_broker() -> int:
    seen = {"allowed": 0, "canary": 0, "udp_canary": 0}

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["allowed"] += 1
            body = (b"<html><body><div id='rendered'>pending</div>"
                    b"<script>document.getElementById('rendered').textContent="
                    b"'javascript-done';fetch('http://canary.test:9/x')"
                    b".catch(()=>{});</script></body></html>")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    class Canary(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["canary"] += 1
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    canary = ThreadingHTTPServer(("0.0.0.0", 0), Canary)
    udp_canary = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_canary.bind(("0.0.0.0", canary.server_port))
    udp_canary.settimeout(0.1)

    def serve_udp():
        while not udp_stopping.is_set():
            try:
                data, _ = udp_canary.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                return
            if data:
                seen["udp_canary"] += 1

    udp_stopping = threading.Event()
    udp_thread = threading.Thread(target=serve_udp, daemon=True)
    fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    canary_thread = threading.Thread(target=canary.serve_forever, daemon=True)
    udp_thread.start()
    fixture_thread.start()
    canary_thread.start()

    class Policy(DestinationPolicy):
        def port_allowed(self, scheme, port):
            return scheme == "http" and port == fixture.server_port

        def address_allowed(self, ip):
            return ip == "127.0.0.1"

        def resolve_target(self, raw_target, *, connect, resolver):
            target, ip = super().resolve_target(raw_target, connect=connect,
                                                resolver=resolver)
            if target.host != "fixture.test":
                raise PolicyError("host_denied")
            return target, ip

    # Docker container start is outside the per-operation clock in this local
    # harness. The worker below still gets one absolute 25-second deadline.
    deadline = time.monotonic() + 90
    broker = Broker(Policy(), resolver=lambda *_: ["127.0.0.1"],
                    total_deadline=deadline)
    bridge = None
    result = {"status": "fail", "reason": "incomplete"}
    try:
        address = broker.start()
        bridge = BridgeServer(str(ROOT / "relay.sock"), address,
                              operation_id=OPERATION_ID, deadline=deadline,
                              allowed_uid=10001)
        bridge.start()
        _write_bounded(ROOT / "ready.json", {
            "fixture_port": fixture.server_port,
            "canary_port": canary.server_port,
            "canary_ip": socket.gethostbyname(socket.gethostname()),
        })
        while time.monotonic() < deadline and not (ROOT / "done").exists():
            time.sleep(0.05)
        result = {"status": "observed" if (ROOT / "done").exists() else "timeout",
                  "fixture_hits": seen["allowed"], "canary_hits": seen["canary"],
                  "udp_canary_hits": seen["udp_canary"],
                  "broker_allowed": broker.allowed, "broker_denied": broker.denied,
                  "broker_requests": broker.operation_requests,
                  "broker_bytes": broker.operation_bytes,
                  "handshake_denied": bridge.denied_handshakes}
    finally:
        if bridge is not None and not bridge.close():
            result["status"] = "cleanup_uncertain"
        if not broker.close():
            result["status"] = "cleanup_uncertain"
        fixture.shutdown()
        canary.shutdown()
        fixture.server_close()
        canary.server_close()
        udp_stopping.set()
        udp_canary.close()
        fixture_thread.join(timeout=2)
        canary_thread.join(timeout=2)
        udp_thread.join(timeout=2)
        if (fixture_thread.is_alive() or canary_thread.is_alive() or
                udp_thread.is_alive()):
            result["status"] = "cleanup_uncertain"
        _write_bounded(ROOT / "broker-result.json", result)
        print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "observed" else 1


def run_worker(*, relay_only: bool = False) -> int:
    result = {"status": "fail", "reason": "incomplete"}
    try:
        inspect_boundary()
        if "SYNTHETIC_SECRET" in os.environ or b"SYNTHETIC_SECRET" in Path(
                "/proc/self/environ").read_bytes():
            raise WorkerBoundaryError("worker_environment")
        ready = json.loads((ROOT / "ready.json").read_text(encoding="ascii"))
        port = ready["fixture_port"]
        canary_port = ready["canary_port"]
        canary_ip = ready["canary_ip"]
        if (not isinstance(port, int) or not 1 <= port <= 65535 or
                not isinstance(canary_port, int) or not 1 <= canary_port <= 65535 or
                not isinstance(canary_ip, str) or
                not ipaddress.ip_address(canary_ip).is_private or
                ipaddress.ip_address(canary_ip).is_loopback):
            raise WorkerBoundaryError("worker_protocol")
        direct_tcp = _direct_tcp_denied(canary_ip, canary_port)
        direct_udp = _direct_udp_denied(canary_ip, canary_port)
        if not direct_tcp or not direct_udp:
            raise WorkerBoundaryError("worker_direct_network")
        deadline = time.monotonic() + 45
        if relay_only:
            relay = WorkerRelay(str(ROOT / "relay.sock"),
                                operation_id=OPERATION_ID, deadline=deadline)
            address = relay.start()
            try:
                client = http.client.HTTPConnection(*address, timeout=5)
                client.request("GET", f"http://fixture.test:{port}/")
                response = client.getresponse()
                body = response.read(2048)
                client.close()
                if response.status != 200 or b"javascript-done" not in body:
                    raise WorkerBoundaryError("worker_relay_result")
            finally:
                if not relay.close():
                    raise WorkerBoundaryError("worker_relay_cleanup_uncertain")
            result = {"status": "pass", "reason": "relay_only",
                      "direct_tcp_denied": direct_tcp,
                      "direct_udp_denied": direct_udp}
        else:
            page = render_in_isolated_worker(
                f"http://fixture.test:{port}/", socket_path=str(ROOT / "relay.sock"),
                operation_id=OPERATION_ID, chromium="/usr/bin/chromium",
                deadline=deadline, max_chars=10000,
            )
            rendered = "javascript-done" in page.get("content", "")
            result = {"status": "pass" if rendered else "fail",
                      "reason": "rendered" if rendered else "not_rendered",
                      "direct_tcp_denied": direct_tcp, "direct_udp_denied": direct_udp,
                      "render_method": page.get("render_method")}
    except Exception as error:
        result = {"status": "fail", "reason": getattr(error, "code", "worker_failure")}
    finally:
        (ROOT / "done").touch()
        print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


def _direct_tcp_denied(ip: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((ip, port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def _direct_udp_denied(ip: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    try:
        sock.connect((ip, port))
        sock.send(b"synthetic")
        return False
    except OSError:
        return True
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(run_broker() if sys.argv[1:] == ["broker"] else
             run_worker(relay_only=sys.argv[1:] == ["relay-only"]))
