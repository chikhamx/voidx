"""Retry settings helpers."""

from __future__ import annotations

from typing import Any

from voidx.config.models import RetryConfig


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
        except Exception:
            return RetryConfig()

    def set_retry_config(self, config: RetryConfig):
        return self._set_setting("retry", config.model_dump())

    def _retry_data(self) -> dict[str, Any]:
        data = self._effective_data().get("retry", {})
        return dict(data) if isinstance(data, dict) else {}
