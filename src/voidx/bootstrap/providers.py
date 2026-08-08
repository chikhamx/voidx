"""Explicit composition of built-in LLM provider specifications."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.llm.adapters.http_model_discovery import (
    HttpModelDiscovery,
    catalog_log_event,
)
from voidx.llm.application.model_catalog import ModelCatalog
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.catalog import PROVIDER_SPECS

if TYPE_CHECKING:
    from voidx.config import Settings


def build_provider_specs() -> tuple[ProviderSpec, ...]:
    return PROVIDER_SPECS


def build_model_catalog(settings: Settings | None, log_event=None) -> ModelCatalog:
    """Build a catalog scoped to the owning workspace/application."""
    log_event = log_event or catalog_log_event
    return ModelCatalog(
        provider_specs=build_provider_specs(),
        settings=settings,
        discovery=HttpModelDiscovery(log_event=log_event),
        log_event=log_event,
    )


__all__ = ["build_model_catalog", "build_provider_specs"]
