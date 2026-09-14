"""Closed V1.11 payment definitions for the eight approved variants."""

from x402.http import PaymentOption
from x402.http.types import RouteConfig

from .payments import X402Settings
from .v111_contracts import V111_VARIANTS


def build_v111_http_routes(settings: X402Settings) -> dict[str, RouteConfig]:
    """Return only the eight fixed, price-discriminated REST routes."""
    if not settings.enabled:
        return {}
    return {
        f"POST {definition.rest_path}": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=settings.pay_to,
                price=definition.price,
                network=settings.network,
            ),
            description=(
                f"SmartFetch {definition.capability} {definition.variant}"
            ),
            mime_type="application/json",
            service_name="SmartFetch",
        )
        for definition in V111_VARIANTS
    }


__all__ = ["build_v111_http_routes"]
