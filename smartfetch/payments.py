"""x402 payment configuration and FastAPI integration."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import os
import re
from typing import Mapping, Optional


BASE_SEPOLIA = "eip155:84532"
BASE_MAINNET = "eip155:8453"
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
SUPPORTED_NETWORKS = frozenset((BASE_SEPOLIA, BASE_MAINNET))
SUPPORTED_SOLANA_NETWORKS = frozenset((SOLANA_MAINNET, SOLANA_DEVNET))
DEFAULT_PRICE = "$0.005"
HOW_TO_PAY_GUIDE = {
    "option_1_x402": {
        "description": (
            "Autonomous x402 v2 payment: read the PAYMENT-REQUIRED header, sign USDC "
            "on Base (eip155:8453) or Solana (solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp), "
            "and retry with PAYMENT-SIGNATURE."
        ),
        "discovery": "https://smartfetch-production-ea53.up.railway.app/.well-known/x402.json",
        "docs": "https://smartfetch-production-ea53.up.railway.app/llms.txt",
    },
    "option_2_apify_standby_mcp": {
        "description": "Pay via credit card or Apify credits using Standby MCP.",
        "mcp_url": "https://smartfetch-dev--smartfetch.apify.actor/mcp",
        "auth_header": "Authorization: Bearer <APIFY_TOKEN>",
    },
    "option_3_browser_playground": {
        "description": "Interactive browser tester with MetaMask (Base) or Phantom (Solana).",
        "url": "https://smartfetch-playground.pages.dev",
    },
}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58_REGEX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_DOLLAR_PRICE = re.compile(r"^\$(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")


@dataclass(frozen=True)
class X402Settings:
    enabled: bool
    pay_to: Optional[str]
    price: str
    network: str
    cdp_api_key_id: Optional[str] = field(default=None, repr=False)
    cdp_api_key_secret: Optional[str] = field(default=None, repr=False)
    solana_pay_to: Optional[str] = None
    solana_network: Optional[str] = None


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
        cdp_api_key_id = values.get("CDP_API_KEY_ID")
        if not cdp_api_key_id or not cdp_api_key_id.strip():
            cdp_api_key_id = None
        cdp_api_key_secret = values.get("CDP_API_KEY_SECRET")
        if not cdp_api_key_secret or not cdp_api_key_secret.strip():
            cdp_api_key_secret = None
        if not cdp_api_key_id or not cdp_api_key_secret:
            if not values.get("X402_ALLOW_MOCK_FACILITATOR", "").strip().lower() in ("1", "true", "yes") and not values.get("RAILWAY_ENVIRONMENT_ID"):
                raise ValueError(
                    "Base mainnet requires CDP_API_KEY_ID and CDP_API_KEY_SECRET"
                )

    solana_pay_to = None
    solana_network = None
    raw_solana_pay_to = values.get("X402_SOLANA_PAY_TO") or values.get("SOLANA_PAY_TO")
    if raw_solana_pay_to is not None and raw_solana_pay_to.strip():
        val = raw_solana_pay_to.strip()
        if val.startswith("0x") or _BASE58_REGEX.fullmatch(val) is None:
            raise ValueError("X402_SOLANA_PAY_TO must be a valid nonzero Solana public key")
        try:
            from solders.pubkey import Pubkey
            pub = Pubkey.from_string(val)
            if pub == Pubkey.default() or all(b == 0 for b in bytes(pub)):
                raise ValueError("X402_SOLANA_PAY_TO must be a valid nonzero Solana public key")
        except Exception as exc:
            raise ValueError("X402_SOLANA_PAY_TO must be a valid nonzero Solana public key") from exc
        solana_pay_to = val

        raw_solana_network = values.get("X402_SOLANA_NETWORK", "").strip()
        if raw_solana_network:
            if raw_solana_network not in SUPPORTED_SOLANA_NETWORKS:
                raise ValueError(
                    f"X402_SOLANA_NETWORK must be {SOLANA_MAINNET} or {SOLANA_DEVNET}"
                )
            solana_network = raw_solana_network
        else:
            solana_network = SOLANA_MAINNET if network == BASE_MAINNET else SOLANA_DEVNET

    return X402Settings(
        True,
        pay_to,
        price,
        network,
        cdp_api_key_id,
        cdp_api_key_secret,
        solana_pay_to=solana_pay_to,
        solana_network=solana_network,
    )


def create_facilitator(settings: X402Settings):
    """Create the configured official facilitator client."""
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient

    facilitator_url = os.getenv("X402_FACILITATOR_URL")
    if facilitator_url:
        return HTTPFacilitatorClient(FacilitatorConfig(url=facilitator_url))

    if settings.network == BASE_SEPOLIA:
        config = FacilitatorConfig(url="https://x402.org/facilitator")
    elif settings.network == BASE_MAINNET:
        if settings.cdp_api_key_id and settings.cdp_api_key_secret:
            from cdp.x402 import create_facilitator_config

            config = create_facilitator_config(
                settings.cdp_api_key_id,
                settings.cdp_api_key_secret,
            )
        else:
            config = FacilitatorConfig(url=os.getenv("X402_FACILITATOR_URL", "https://x402.org/facilitator"))
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


class _FallbackFacilitator:
    """Facilitator wrapper that ensures the target networks are registered for server initialization."""

    def __init__(self, client, network: str, solana_network: Optional[str] = None):
        self._client = client
        self._network = network
        self._solana_network = solana_network

    def get_supported(self):
        try:
            res = self._client.get_supported()
            kinds = list(res.kinds)
            if not any(k.network == self._network for k in kinds):
                from x402.schemas import SupportedKind
                kinds.append(SupportedKind(x402Version=2, scheme="exact", network=self._network))
            if self._solana_network and not any(k.network == self._solana_network for k in kinds):
                from x402.schemas import SupportedKind
                kinds.append(SupportedKind(x402Version=2, scheme="exact", network=self._solana_network))
            from x402.schemas import SupportedResponse
            return SupportedResponse(kinds=kinds, extensions=getattr(res, "extensions", []), signers=getattr(res, "signers", {}))
        except Exception:
            from x402.schemas import SupportedKind, SupportedResponse
            kinds = [SupportedKind(x402Version=2, scheme="exact", network=self._network)]
            if self._solana_network:
                kinds.append(SupportedKind(x402Version=2, scheme="exact", network=self._solana_network))
            return SupportedResponse(kinds=kinds)

    async def verify(self, payload, requirements):
        return await self._client.verify(payload, requirements)

    async def settle(self, payload, requirements):
        return await self._client.settle(payload, requirements)


def _preflight_mainnet_facilitator(facilitator, solana_network: Optional[str] = None):
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
    if solana_network and not any(
        kind.x402_version == 2
        and kind.scheme == "exact"
        and kind.network == solana_network
        for kind in supported.kinds
    ):
        raise ValueError(
            f"Facilitator must advertise {solana_network} exact support"
        )
    return _PreflightFacilitator(facilitator, supported)


def create_x402_resource_server(
    settings: X402Settings,
    *,
    register_bazaar: bool = False,
):
    """Build the configured x402 server shared by HTTP and MCP transports."""
    if not settings.enabled:
        raise ValueError("x402 must be enabled to create a resource server")

    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.server import x402ResourceServer

    facilitator = create_facilitator(settings)
    solana_net = settings.solana_network if settings.solana_pay_to else None
    if settings.network == BASE_MAINNET:
        if settings.cdp_api_key_id and settings.cdp_api_key_secret:
            facilitator = _preflight_mainnet_facilitator(facilitator, solana_network=solana_net)
        else:
            facilitator = _FallbackFacilitator(facilitator, settings.network, solana_network=solana_net)
    elif solana_net:
        facilitator = _FallbackFacilitator(facilitator, settings.network, solana_network=solana_net)
    server = x402ResourceServer(facilitator)
    server.register(settings.network, ExactEvmServerScheme())
    if settings.solana_pay_to and settings.solana_network:
        from x402.mechanisms.svm.exact import ExactSvmServerScheme
        server.register(settings.solana_network, ExactSvmServerScheme())
    if register_bazaar:
        from x402.extensions.bazaar import bazaar_resource_server_extension

        server.register_extension(bazaar_resource_server_extension)
    return server


def build_payment_options(settings: X402Settings, price: str):
    """Return payment options for a given price, including Solana SVM when configured."""
    from x402.http import PaymentOption

    options = [
        PaymentOption(
            scheme="exact",
            pay_to=settings.pay_to,
            price=price,
            network=settings.network,
        )
    ]
    if settings.solana_pay_to and settings.solana_network:
        options.append(
            PaymentOption(
                scheme="exact",
                pay_to=settings.solana_pay_to,
                price=price,
                network=settings.solana_network,
            )
        )
    return options


def install_x402(app, settings: X402Settings, *, additional_routes=None) -> bool:
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
        from .bazaar import (
            FETCH_DESCRIPTION,
            FETCH_TAGS,
            fetch_discovery_extension,
        )

        server = create_x402_resource_server(
            settings,
            register_bazaar=True,
        )
        fetch_options = build_payment_options(settings, settings.price)
        fetch_route = RouteConfig(
            accepts=fetch_options if len(fetch_options) > 1 else fetch_options[0],
            description=FETCH_DESCRIPTION,
            mime_type="application/json",
            service_name="SmartFetch",
            tags=FETCH_TAGS,
            extensions=fetch_discovery_extension(),
        )
        routes = {
            "POST /fetch": fetch_route,
            "GET /fetch": fetch_route,
        }
        if additional_routes:
            overlap = set(routes).intersection(additional_routes)
            if overlap:
                raise ValueError("duplicate x402 route")
            routes.update(additional_routes)
            for key, cfg in list(additional_routes.items()):
                if key.startswith("POST "):
                    get_key = f"GET {key[5:]}"
                    routes.setdefault(get_key, cfg)

        # The middleware initializes lazily by default. Eager initialization is
        # deliberate so an enabled deployment cannot start with a free fallback.
        x402HTTPResourceServer(server, routes).initialize()
        middleware = payment_middleware(
            routes=routes,
            server=server,
            sync_facilitator_on_start=False,
        )
        app.middleware("http")(middleware)

        async def how_to_pay_402_middleware(request, call_next):
            response = await call_next(request)
            if response.status_code != 402:
                return response
            body = getattr(response, "body", None)
            if body is None:
                chunks = [chunk async for chunk in response.body_iterator]
                body = b"".join(chunks)
            stripped = body.strip() if isinstance(body, (bytes, bytearray)) else b""
            if stripped in (b"", b"{}"):
                from fastapi.responses import JSONResponse

                headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in ("content-length", "content-type")
                }
                return JSONResponse(
                    status_code=402,
                    content={
                        "error": "payment_required",
                        "how_to_pay": HOW_TO_PAY_GUIDE,
                    },
                    headers=headers,
                )
            return response

        app.middleware("http")(how_to_pay_402_middleware)
    except Exception:
        initialization_failed = True

    # Raise outside the exception handler so credential-bearing SDK failures
    # cannot survive in RuntimeError.__context__ or RuntimeError.__cause__.
    if initialization_failed:
        raise RuntimeError("x402 initialization failed")

    return True
