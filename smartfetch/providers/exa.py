"""Bounded Exa Search API adapter for V1.11."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import ipaddress
import json
import re
import socket
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from ..costs import ProviderUsage, UsageAccountingError
from ..provider_controls import RequestCostBudget, exa_cost_micro_usd
from ..provider_health import (
    ProviderAdapterError,
    ProviderCircuitBreaker,
    ProviderConfig,
)
from ..provider_types import SearchCandidate, SearchProviderResult, SearchRequest


ExaSearchMode = Literal["results", "answer", "structured"]
EXA_SEARCH_ENDPOINT = "https://api.exa.ai/search"


@dataclass(frozen=True, slots=True)
class ExaHTTPResponse:
    status_code: int
    body: bytes


class ExaTransport(Protocol):
    async def search(
        self,
        *,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> ExaHTTPResponse: ...


class RequestsExaTransport:
    """Cancellable Exa HTTPS transport with streaming bounds and no retry."""

    def __init__(self, http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http_transport = http_transport

    async def search(
        self,
        *,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> ExaHTTPResponse:
        timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        async with httpx.AsyncClient(
            transport=self._http_transport,
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                EXA_SEARCH_ENDPOINT,
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16_384):
                    if len(body) + len(chunk) > max_response_bytes:
                        raise ProviderAdapterError("invalid_provider_output", provider="exa")
                    body.extend(chunk)
                return ExaHTTPResponse(response.status_code, bytes(body))


class ExaSearchProvider:
    """Per-request SearchProvider bound to one of the three finite search modes."""

    def __init__(
        self,
        *,
        mode: ExaSearchMode,
        config: ProviderConfig,
        circuit: ProviderCircuitBreaker,
        budget: RequestCostBudget,
        transport: ExaTransport | None = None,
    ) -> None:
        if type(mode) is not str or mode not in {"results", "answer", "structured"}:
            raise ValueError("invalid_provider_config")
        if type(config) is not ProviderConfig or config.provider != "exa":
            raise ValueError("invalid_provider_config")
        if (
            type(circuit) is not ProviderCircuitBreaker
            or circuit.provider != "exa"
            or type(budget) is not RequestCostBudget
        ):
            raise ValueError("invalid_provider_config")
        self._mode = mode
        self._config = config
        self._circuit = circuit
        self._budget = budget
        self._transport = transport or RequestsExaTransport()

    def require_ready(self) -> None:
        self._config.require_configured()
        if self._config.max_cost_micro_usd == 0:
            raise ProviderAdapterError("capacity_unavailable", provider="exa")
        self._circuit.require_available()

    async def search(self, request: SearchRequest) -> SearchProviderResult:
        payload = _build_payload(request)
        self.require_ready()
        reservation = self._budget.reserve(
            provider="exa",
            maximum_micro_usd=self._config.max_cost_micro_usd,
        )
        try:
            permit = self._circuit.begin_call()
        except BaseException:
            self._budget.release(reservation)
            raise
        try:
            response = await asyncio.wait_for(
                self._transport.search(
                    api_key=self._config.api_key,  # type: ignore[arg-type]
                    payload=payload,
                    timeout_seconds=float(self._config.timeout_seconds),
                    max_response_bytes=self._config.max_response_bytes,
                ),
                timeout=float(self._config.timeout_seconds),
            )
            result = _normalize_response(response, maximum=self._config.max_response_bytes)
            self._budget.commit(reservation, actual_micro_usd=result.usage.cost_micro_usd)
        except asyncio.CancelledError:
            _release_quietly(self._budget, reservation)
            self._circuit.record_failure(permit)
            raise
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException) as exc:
            _release_quietly(self._budget, reservation)
            self._circuit.record_failure(permit)
            raise ProviderAdapterError("provider_timeout", provider="exa", cause=exc) from None
        except ProviderAdapterError:
            _release_quietly(self._budget, reservation)
            self._circuit.record_failure(permit)
            raise
        except (httpx.RequestError, TypeError, ValueError, UnicodeError, OverflowError) as exc:
            _release_quietly(self._budget, reservation)
            self._circuit.record_failure(permit)
            raise ProviderAdapterError("search_failed", provider="exa", cause=exc) from None
        self._circuit.record_success(permit)
        return result


def _release_quietly(budget: RequestCostBudget, reservation: object) -> None:
    try:
        budget.release(reservation)  # type: ignore[arg-type]
    except ValueError:
        pass


def _build_payload(request: SearchRequest) -> dict[str, Any]:
    if (
        type(request) is not SearchRequest
        or type(request.query) is not str
        or not 1 <= len(request.query.strip()) <= 500
        or type(request.max_results) is not int
        or not 1 <= request.max_results <= 10
        or type(request.domains) is not tuple
        or len(request.domains) > 10
        or any(type(domain) is not str or not domain or len(domain) > 253 for domain in request.domains)
        or (request.freshness_after is not None and not _valid_timestamp(request.freshness_after))
    ):
        raise ProviderAdapterError("invalid_provider_output", provider="exa")
    payload: dict[str, Any] = {
        "query": request.query.strip(),
        "type": "auto",
        "numResults": request.max_results,
        "contents": {"highlights": {"maxCharacters": 800}},
    }
    if request.domains:
        payload["includeDomains"] = list(request.domains)
    if request.freshness_after is not None:
        payload["startPublishedDate"] = request.freshness_after
    return payload


def _normalize_response(response: ExaHTTPResponse, *, maximum: int) -> SearchProviderResult:
    if (
        type(response) is not ExaHTTPResponse
        or type(response.status_code) is not int
        or type(response.body) is not bytes
    ):
        raise ProviderAdapterError("invalid_provider_output", provider="exa")
    if len(response.body) > maximum:
        raise ProviderAdapterError("invalid_provider_output", provider="exa")
    if response.status_code in {401, 403}:
        raise ProviderAdapterError("provider_unavailable", provider="exa")
    if response.status_code == 429:
        raise ProviderAdapterError("capacity_unavailable", provider="exa")
    if response.status_code in {408, 504}:
        raise ProviderAdapterError("provider_timeout", provider="exa")
    if response.status_code != 200:
        raise ProviderAdapterError("search_failed", provider="exa")
    try:
        raw = json.loads(response.body.decode("utf-8"), parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise ProviderAdapterError("invalid_provider_output", provider="exa") from None
    if type(raw) is not dict or type(raw.get("results")) is not list or len(raw["results"]) > 10:
        raise ProviderAdapterError("invalid_provider_output", provider="exa")
    cost = raw.get("costDollars")
    if type(cost) is not dict:
        raise ProviderAdapterError("invalid_provider_output", provider="exa")
    try:
        cost_micro_usd = exa_cost_micro_usd(cost.get("total"))
    except UsageAccountingError:
        raise ProviderAdapterError("invalid_provider_output", provider="exa") from None
    candidates = tuple(_candidate(item, rank) for rank, item in enumerate(raw["results"], 1))
    return SearchProviderResult(
        candidates=candidates,
        usage=ProviderUsage(provider="exa", search_queries=1, cost_micro_usd=cost_micro_usd),
    )


def _candidate(raw: object, rank: int) -> SearchCandidate:
    if type(raw) is not dict:
        raise ProviderAdapterError("invalid_provider_output", provider="exa")
    title = raw.get("title", "")
    url = raw.get("url")
    highlights = raw.get("highlights", [])
    published = raw.get("publishedDate")
    if (
        type(title) is not str
        or type(url) is not str
        or not 1 <= len(url) <= 4096
        or type(highlights) is not list
        or len(highlights) > 10
        or any(type(item) is not str for item in highlights)
        or (published is not None and type(published) is not str)
        or not _safe_https_url(url)
        or not _valid_timestamp(published)
    ):
        raise ProviderAdapterError("invalid_provider_output", provider="exa")
    snippet = " ".join(highlights)[:800]
    return SearchCandidate(
        source_id=f"s{rank}",
        rank=rank,
        title=title[:300],
        url=url,
        snippet=snippet,
        published_at=published,
    )


def _safe_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.port not in {None, 443}:
            return False
        host = parsed.hostname.rstrip(".").lower()
        ascii_host = host.encode("idna").decode("ascii")
        if len(ascii_host) > 253 or "." not in ascii_host:
            return False
        if any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in ascii_host.split(".")
        ):
            return False
        if ascii_host.endswith((".local", ".invalid")):
            return False
        try:
            ipaddress.ip_address(ascii_host)
        except ValueError:
            try:
                socket.inet_aton(ascii_host)
            except OSError:
                return True
            return False
        return False
    except (ValueError, UnicodeError):
        return False


def _valid_timestamp(value: str | None) -> bool:
    if value is None:
        return True
    if len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


__all__ = [
    "EXA_SEARCH_ENDPOINT",
    "ExaHTTPResponse",
    "ExaSearchMode",
    "ExaSearchProvider",
    "ExaTransport",
    "RequestsExaTransport",
]
