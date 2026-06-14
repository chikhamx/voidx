"""Agent step-limit settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.config.models import ParallelSubagentsConfig


class SettingsAgentMixin:
    def get_parallel_subagents(self) -> ParallelSubagentsConfig:
        raw = self._data.get("parallel_subagents", {})
        if not isinstance(raw, dict):
            raw = {}
        return ParallelSubagentsConfig(**{
            k: v for k, v in raw.items()
            if k in ParallelSubagentsConfig.model_fields
        })

    def set_parallel_subagents(self, config: ParallelSubagentsConfig) -> Path:
        self._data["parallel_subagents"] = config.model_dump()
        self._save()
        return self._path
