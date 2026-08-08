"""Pure provider specification types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from voidx.llm.domain.model import ModelConfig

PROTOCOL_DEEPSEEK = "deepseek"

ReasoningHook = Callable[[ModelConfig], dict]
TemperatureHook = Callable[[ModelConfig], float | None]


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    protocol: str
    default_base_url: str = ""
    context_limit: int = 0
    static_models: tuple[str, ...] = ()
    reasoning: ReasoningHook | None = None
    temperature_override: TemperatureHook | None = None
