"""Benchmark-controlled Gemini model routing for V1.11."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


GeminiModelId = Literal["gemini-3.8-flash", "gemini-3.5-flash-lite"]
GeminiWorkload = Literal[
    "answer",
    "structured_search",
    "webpage",
    "image",
    "pdf",
    "audio",
    "video",
]

APPROVED_GEMINI_MODELS: tuple[GeminiModelId, ...] = (
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
)
GEMINI_WORKLOADS: tuple[GeminiWorkload, ...] = (
    "answer",
    "structured_search",
    "webpage",
    "image",
    "pdf",
    "audio",
    "video",
)
_OUTPUT_TOKEN_CAPS: Mapping[str, int] = MappingProxyType(
    {
        "answer": 4096,
        "structured_search": 8192,
        "webpage": 8192,
        "image": 8192,
        "pdf": 8192,
        "audio": 8192,
        "video": 8192,
    }
)


class ModelRoutingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModelSelection:
    model_id: GeminiModelId
    route_label: Literal["flash", "flash_lite"]
    thinking_level: Literal["low"]
    max_output_tokens: int


class BenchmarkModelRouter:
    """Owns a complete benchmark result; no route is selected by default."""

    def __init__(
        self,
        *,
        routes: dict[str, str] | None = None,
        override_model_id: str | None = None,
    ) -> None:
        if override_model_id is not None and (
            type(override_model_id) is not str or override_model_id not in APPROVED_GEMINI_MODELS
        ):
            raise ModelRoutingError("invalid_model_routing")
        if routes is None:
            owned: dict[str, str] = {}
        else:
            if type(routes) is not dict or len(routes) != len(GEMINI_WORKLOADS):
                raise ModelRoutingError("invalid_model_routing")
            owned = {}
            for workload, model_id in routes.items():
                if (
                    type(workload) is not str
                    or workload not in GEMINI_WORKLOADS
                    or type(model_id) is not str
                    or model_id not in APPROVED_GEMINI_MODELS
                ):
                    raise ModelRoutingError("invalid_model_routing")
                owned[workload] = model_id
            if len(owned) != len(GEMINI_WORKLOADS):
                raise ModelRoutingError("invalid_model_routing")
        self._routes = MappingProxyType(owned)
        self._override = override_model_id

    @property
    def configured(self) -> bool:
        return self._override is not None or bool(self._routes)

    def select(self, workload: object) -> ModelSelection:
        if type(workload) is not str or workload not in GEMINI_WORKLOADS:
            raise ModelRoutingError("invalid_model_routing")
        model_id = self._override or self._routes.get(workload)
        if model_id is None:
            raise ModelRoutingError("model_route_unconfigured")
        return ModelSelection(
            model_id=model_id,
            route_label="flash" if model_id == "gemini-3.8-flash" else "flash_lite",
            thinking_level="low",
            max_output_tokens=_OUTPUT_TOKEN_CAPS[workload],
        )


__all__ = [
    "APPROVED_GEMINI_MODELS",
    "GEMINI_WORKLOADS",
    "BenchmarkModelRouter",
    "GeminiModelId",
    "GeminiWorkload",
    "ModelRoutingError",
    "ModelSelection",
]
