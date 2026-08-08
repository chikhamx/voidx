"""Compaction summary settings helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from voidx.config.models import CompactionConfig

logger = logging.getLogger(__name__)


class SettingsCompactionMixin:
    def get_compaction_config(self) -> CompactionConfig:
        raw = self._effective_data().get("compaction", {})
        try:
            if not isinstance(raw, dict):
                raise TypeError("expected an object")
            return CompactionConfig.model_validate(raw)
        except Exception as exc:
            logger.warning("Invalid compaction settings; using defaults: %s", exc)
            return CompactionConfig()

    def set_compaction_config(self, config: CompactionConfig) -> Path:
        return self._set_setting("compaction", config.model_dump(mode="json"))
