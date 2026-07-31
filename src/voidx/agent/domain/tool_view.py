"""Shared bound-tool view for autonomous goal/loop profiles."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from voidx.agent.domain.tool_policy import ToolPolicyDecision


class BoundToolView(BaseModel):
    model_config = ConfigDict(frozen=True)

    bound_tool_ids: frozenset[str] = Field(default_factory=frozenset)

    def allows(self, tool_id: str, **_kwargs) -> bool:
        return tool_id in self.bound_tool_ids

    def visible_tool_ids(self, available_tool_ids) -> frozenset[str]:
        return frozenset(tool for tool in available_tool_ids if self.allows(tool))

    def check_tool_call(self, tool_id: str, _args) -> ToolPolicyDecision:
        allowed = self.allows(tool_id)
        requests_approval = allowed and tool_id == "bash"
        return ToolPolicyDecision(allowed, "tool_bound" if allowed else "tool_not_bound", requests_approval)
