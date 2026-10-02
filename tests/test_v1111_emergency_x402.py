"""The installed official x402 wrappers have no emergency execution entry."""

import base64
import json
import socket
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
import x402
import x402.http.middleware.fastapi as official_http
import x402.http.utils as official_utils
import x402.mcp as official_mcp
import x402.server as official_server
from x402.http.facilitator_client import HTTPFacilitatorClient

from smartfetch import activity, mcp_server, payments, server


INERT_PROOF = base64.b64encode(json.dumps({"x402Version": 2, "payload": {}}).encode()).decode()
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _request(client, surface, proof):
    if surface == "fetch":
        body = {"url": "https://example.com/payment-canary"}
        headers = [("Content-Type", "application/json")]
        if isinstance(proof, tuple):
            headers.extend(proof)
        elif proof is not None:
            headers.append(("PAYMENT-SIGNATURE", proof))
        return client.post("/fetch", json=body, headers=headers)
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": surface, "arguments": {"url": "https://example.com/payment-canary"}},
    }
    headers = list(MCP_HEADERS.items())
    if isinstance(proof, tuple):
        headers.extend(proof)
    elif proof is not None:
        headers.append(("PAYMENT-SIGNATURE", proof))
    return client.post("/mcp", json=payload, headers=headers)


@pytest.mark.parametrize("surface", ["fetch", *mcp_server.MCP_TOOLS])
@pytest.mark.parametrize("proof", [
    None,
    "malformed-proof",
    INERT_PROOF,
    (("PAYMENT-SIGNATURE", "first-canary"), ("payment-signature", "second-canary")),
])
def test_official_x402_220_boundaries_are_never_entered(surface, proof):
    assert x402.__version__ == "2.20.0"
    with (
        patch.object(official_http, "payment_middleware", wraps=official_http.payment_middleware) as http,
        patch.object(official_mcp, "create_payment_wrapper", wraps=official_mcp.create_payment_wrapper) as mcp,
        patch.object(official_server.x402ResourceServer, "initialize", wraps=official_server.x402ResourceServer.initialize) as init,
        patch.object(official_server.x402ResourceServer, "verify_payment", wraps=official_server.x402ResourceServer.verify_payment) as verify,
        patch.object(official_server.x402ResourceServer, "settle_payment", wraps=official_server.x402ResourceServer.settle_payment) as settle,
        patch.object(HTTPFacilitatorClient, "verify", wraps=HTTPFacilitatorClient.verify) as facilitator_verify,
        patch.object(HTTPFacilitatorClient, "settle", wraps=HTTPFacilitatorClient.settle) as facilitator_settle,
        patch.object(official_utils, "decode_payment_signature_header", wraps=official_utils.decode_payment_signature_header) as parse,
        patch.object(payments, "load_x402_settings", wraps=payments.load_x402_settings) as load,
        patch.object(payments, "create_x402_resource_server", wraps=payments.create_x402_resource_server) as resource,
        patch.object(payments, "install_x402", wraps=payments.install_x402) as install,
        patch.object(activity, "emit_activity", wraps=activity.emit_activity) as events,
        patch.object(activity, "emit_v111_activity", wraps=activity.emit_v111_activity) as v111_events,
        patch.object(socket, "create_connection", side_effect=AssertionError("network entered")) as network,
    ):
        app = server.create_app(
            payment_settings=object(),
            v111_runtime=object(),
            v111_environ={"SMARTFETCH_V111_ENABLED": "true"},
        )
        with TestClient(app) as client:
            result = _request(client, surface, proof)
    assert result.status_code == (503 if surface == "fetch" else 200)
    assert b"payment-canary" not in result.content
    assert all("payment" not in key.lower() for key in result.headers)
    for boundary in (http, mcp, init, verify, settle, facilitator_verify,
                     facilitator_settle, parse, load, resource, install, network):
        boundary.assert_not_called()
    assert all(call.args[0] == "unavailable_attempt" for call in events.call_args_list)
    v111_events.assert_not_called()


def test_no_retained_legacy_factory_can_construct_a_paid_resource():
    assert not hasattr(mcp_server, "_obsolete_paid_factory_disabled")
