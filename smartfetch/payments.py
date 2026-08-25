"""x402 payment configuration and FastAPI integration."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Mapping, Optional


BASE_SEPOLIA = "eip155:84532"
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


def load_x402_settings(
    environ: Optional[Mapping[str, str]] = None,
) -> X402Settings:
    """Load and validate the payment configuration without accepting secrets."""
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
    if network != BASE_SEPOLIA:
        raise ValueError("X402_NETWORK must be Base Sepolia (eip155:84532)")

    return X402Settings(True, pay_to, price, network)


def install_x402(app, settings: X402Settings) -> bool:
    """Install and eagerly initialize official x402 protection when enabled."""
    if not settings.enabled:
        return False

    try:
        from x402.http import (
            FacilitatorConfig,
            HTTPFacilitatorClient,
            PaymentOption,
            x402HTTPResourceServer,
        )
        from x402.http.middleware.fastapi import payment_middleware
        from x402.http.types import RouteConfig
        from x402.mechanisms.evm.exact import ExactEvmServerScheme
        from x402.server import x402ResourceServer

        facilitator = HTTPFacilitatorClient(FacilitatorConfig(
            url="https://x402.org/facilitator"
        ))
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
    except Exception as exc:
        raise RuntimeError(f"x402 initialization failed: {exc}") from exc

    return True
