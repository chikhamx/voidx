"""Agent step-limit settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.config.models import AgentMaxSteps, ParallelSubagentsConfig


class SettingsAgentMixin:
    def get_agent_max_steps(self) -> AgentMaxSteps:
        raw = self._data.get("agent_max_steps", {})
        if not isinstance(raw, dict):
            raw = {}
        return AgentMaxSteps(**{k: v for k, v in raw.items() if k in AgentMaxSteps.model_fields})

    def set_agent_max_steps(self, steps: AgentMaxSteps) -> Path:
        self._data["agent_max_steps"] = steps.model_dump()
        self._save()
        return self._path

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
