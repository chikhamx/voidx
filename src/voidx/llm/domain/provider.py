"""Pure provider selection and context-limit rules."""

from __future__ import annotations

from voidx.llm.domain.model import ModelConfig
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.catalog import PROVIDER_SPECS


def find_provider_spec(
    provider: str,
    provider_specs: tuple[ProviderSpec, ...] = PROVIDER_SPECS,
) -> ProviderSpec | None:
    return next((spec for spec in provider_specs if spec.name == provider), None)


def resolve_protocol(
    config: ModelConfig,
    provider_specs: tuple[ProviderSpec, ...] = PROVIDER_SPECS,
) -> str:
    if config.protocol:
        return config.protocol
    spec = find_provider_spec(config.provider, provider_specs)
    return spec.protocol if spec is not None else "openai"


def get_context_limit(
    provider: str,
    protocol: str = "",
    context_window: int | None = None,
    provider_specs: tuple[ProviderSpec, ...] = PROVIDER_SPECS,
) -> int:
    if context_window is not None and context_window > 0:
        return context_window
    spec = find_provider_spec(provider, provider_specs)
    if spec is not None and spec.context_limit:
        return spec.context_limit
    return 200_000
