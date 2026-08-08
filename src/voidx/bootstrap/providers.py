"""Explicit composition of built-in LLM provider specifications."""

from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.catalog import PROVIDER_SPECS


def build_provider_specs() -> tuple[ProviderSpec, ...]:
    return PROVIDER_SPECS


__all__ = ["build_provider_specs"]
