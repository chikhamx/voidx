"""Agent-related settings helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from voidx.config.models import SubagentBudgetConfig

logger = logging.getLogger(__name__)


class SettingsAgentMixin:
    def get_subagent_budget_config(self) -> SubagentBudgetConfig:
        raw = self._effective_data().get("subagent_budget", {})
        try:
            if not isinstance(raw, dict):
                raise TypeError("expected an object")
            return SubagentBudgetConfig.model_validate(raw)
        except Exception as exc:
            logger.warning("Invalid subagent budget settings; using defaults: %s", exc)
            return SubagentBudgetConfig()

    def set_subagent_budget_config(self, config: SubagentBudgetConfig) -> Path:
        return self._set_setting("subagent_budget", config.model_dump(mode="json"))
