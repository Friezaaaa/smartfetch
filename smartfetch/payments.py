"""x402 payment configuration and FastAPI integration."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Mapping, Optional


BASE_SEPOLIA = "eip155:84532"
BASE_MAINNET = "eip155:8453"
SUPPORTED_NETWORKS = frozenset((BASE_SEPOLIA, BASE_MAINNET))
DEFAULT_PRICE = "$0.005"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DOLLAR_PRICE = re.compile(r"^\$(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")


@dataclass(frozen=True)
class X402Settings:
    enabled: bool
    pay_to: Optional[str]
    price: str
    network: str
    cdp_api_key_id: Optional[str] = field(default=None, repr=False)
    cdp_api_key_secret: Optional[str] = field(default=None, repr=False)


def load_x402_settings(
    environ: Optional[Mapping[str, str]] = None,
) -> X402Settings:
    """Load and validate payment configuration without wallet secrets."""
    values = os.environ if environ is None else environ
    enabled_value = values.get("X402_ENABLED", "").strip().lower()
    if enabled_value in _TRUE_VALUES:
        enabled = True
    elif enabled_value in _FALSE_VALUES:
        enabled = False
    else:
        raise ValueError("X402_ENABLED must be a recognized boolean value")

    if not enabled:
        return X402Settings(False, None, DEFAULT_PRICE, BASE_SEPOLIA)

    pay_to = values.get("X402_PAY_TO", "").strip()
    if (
        _EVM_ADDRESS.fullmatch(pay_to) is None
        or int(pay_to[2:], 16) == 0
    ):
        raise ValueError("X402_PAY_TO must be a nonzero EVM receiving address")

    price = values.get("X402_PRICE", DEFAULT_PRICE).strip()
    if _DOLLAR_PRICE.fullmatch(price) is None:
        raise ValueError("X402_PRICE must be a positive dollar amount")
    try:
        positive_price = Decimal(price[1:]) > 0
    except InvalidOperation as exc:
        raise ValueError("X402_PRICE must be a positive dollar amount") from exc
    if not positive_price:
        raise ValueError("X402_PRICE must be a positive dollar amount")

    network = values.get("X402_NETWORK", BASE_SEPOLIA).strip()
    if network not in SUPPORTED_NETWORKS:
        raise ValueError(
            "X402_NETWORK must be Base Sepolia (eip155:84532) "
            "or Base mainnet (eip155:8453)"
        )

    cdp_api_key_id = None
    cdp_api_key_secret = None
    if network == BASE_MAINNET:
        cdp_api_key_id = values.get("CDP_API_KEY_ID", "").strip()
        cdp_api_key_secret = values.get("CDP_API_KEY_SECRET", "")
        if not cdp_api_key_id or not cdp_api_key_secret.strip():
            raise ValueError(
                "Base mainnet requires CDP_API_KEY_ID and CDP_API_KEY_SECRET"
            )

    return X402Settings(
        True,
        pay_to,
        price,
        network,
        cdp_api_key_id,
        cdp_api_key_secret,
    )


def create_facilitator(settings: X402Settings):
    """Create the configured official facilitator client."""
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient

    if settings.network == BASE_SEPOLIA:
        config = FacilitatorConfig(url="https://x402.org/facilitator")
    elif settings.network == BASE_MAINNET:
        if not settings.cdp_api_key_id or not settings.cdp_api_key_secret:
            raise ValueError("Base mainnet CDP credentials are required")
        from cdp.x402 import create_facilitator_config

        config = create_facilitator_config(
            settings.cdp_api_key_id,
            settings.cdp_api_key_secret,
        )
    else:
        raise ValueError("Unsupported x402 network")

    return HTTPFacilitatorClient(config)


class _PreflightFacilitator:
    """Reuse one validated capability response during server initialization."""

    def __init__(self, client, supported):
        self._client = client
        self._supported = supported

    def get_supported(self):
        return self._supported

    async def verify(self, payload, requirements):
        return await self._client.verify(payload, requirements)

    async def settle(self, payload, requirements):
        return await self._client.settle(payload, requirements)


def _preflight_mainnet_facilitator(facilitator):
    supported = facilitator.get_supported()
    if not any(
        kind.x402_version == 2
        and kind.scheme == "exact"
        and kind.network == BASE_MAINNET
        for kind in supported.kinds
    ):
        raise ValueError(
            "Facilitator must advertise Base mainnet exact support"
        )
    return _PreflightFacilitator(facilitator, supported)


def install_x402(app, settings: X402Settings) -> bool:
    """Install and eagerly initialize official x402 protection when enabled."""
    if not settings.enabled:
        return False

    initialization_failed = False
    try:
        from x402.http import (
            PaymentOption,
            x402HTTPResourceServer,
        )
        from x402.http.middleware.fastapi import payment_middleware
        from x402.http.types import RouteConfig
        from x402.mechanisms.evm.exact import ExactEvmServerScheme
        from x402.server import x402ResourceServer

        facilitator = create_facilitator(settings)
        if settings.network == BASE_MAINNET:
            facilitator = _preflight_mainnet_facilitator(facilitator)
        server = x402ResourceServer(facilitator)
        server.register(settings.network, ExactEvmServerScheme())
        routes = {
            "POST /fetch": RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to=settings.pay_to,
                    price=settings.price,
                    network=settings.network,
                ),
                description="SmartFetch structured web retrieval",
                mime_type="application/json",
            )
        }

        # The middleware initializes lazily by default. Eager initialization is
        # deliberate so an enabled deployment cannot start with a free fallback.
        x402HTTPResourceServer(server, routes).initialize()
        middleware = payment_middleware(
            routes=routes,
            server=server,
            sync_facilitator_on_start=False,
        )
        app.middleware("http")(middleware)
    except Exception:
        initialization_failed = True

    # Raise outside the exception handler so credential-bearing SDK failures
    # cannot survive in RuntimeError.__context__ or RuntimeError.__cause__.
    if initialization_failed:
        raise RuntimeError("x402 initialization failed")

    return True
