"""Provider registry — declarative specs for built-in LLM providers.

Each built-in provider module registers a :class:`ProviderSpec` at import
time.  ``voidx.llm.provider`` (factory) and ``voidx.llm.catalog`` (model
discovery) read this registry instead of keeping per-provider tables.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from voidx.llm.domain.model import ModelConfig

# Protocol shared by China-domestic OpenAI-compatible providers.
PROTOCOL_DEEPSEEK = "deepseek"

ReasoningHook = Callable[[ModelConfig], dict]
TemperatureHook = Callable[[ModelConfig], float | None]


@dataclass(frozen=True)
class ProviderSpec:
    """Static metadata for one built-in provider.

    ``reasoning`` maps unified effort values to the provider's request
    format.  ``temperature_override`` returns a forced temperature (or
    ``None`` to suppress it) when the provider/model requires a specific
    value.  ``context_limit`` of 0 falls back to protocol defaults.
    """

    name: str
    protocol: str
    default_base_url: str = ""
    context_limit: int = 0
    static_models: tuple[str, ...] = ()
    reasoning: ReasoningHook | None = None
    temperature_override: TemperatureHook | None = None


_SPECS: dict[str, ProviderSpec] = {}


def register(spec: ProviderSpec) -> None:
    _SPECS[spec.name] = spec


def get(name: str) -> ProviderSpec | None:
    return _SPECS.get(name)


def all_specs() -> list[ProviderSpec]:
    return list(_SPECS.values())
