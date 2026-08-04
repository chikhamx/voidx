"""Retry settings helpers."""

from __future__ import annotations

import logging
from typing import Any

from voidx.config.models import RetryConfig

logger = logging.getLogger(__name__)


class SettingsRetryMixin:
    def get_retry_config(self) -> RetryConfig:
        data = self._retry_data()
        if not isinstance(data, dict) or not data:
            return RetryConfig()
        try:
            return RetryConfig(**{
                k: v for k, v in data.items()
                if k in {"max_attempts", "base_delay", "max_delay", "jitter"}
            })
        except Exception as exc:
            logger.warning("Invalid retry settings; using defaults: %s", exc)
            return RetryConfig()

    def set_retry_config(self, config: RetryConfig):
        return self._set_setting("retry", config.model_dump())

    def _retry_data(self) -> dict[str, Any]:
        data = self._effective_data().get("retry", {})
        return dict(data) if isinstance(data, dict) else {}
