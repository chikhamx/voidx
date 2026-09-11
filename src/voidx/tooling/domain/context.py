"""Immutable values shared by tool executions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from voidx.tooling.domain.authorization import AuthorizationContext
from voidx.tooling.domain.risk import ApprovedToolRisk


class ToolExecutionContext(BaseModel):
    workspace: str
    session_id: str = "default"
    persona: str = "voidx"
    interaction_mode: str = "auto"
    turn_count: int = 0
    approved_tool_risks: tuple[ApprovedToolRisk, ...] = Field(default_factory=tuple)
    authorization: AuthorizationContext = Field(default_factory=AuthorizationContext)
    model_config = ConfigDict(frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_authorization(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        legacy = {
            key: data[key]
            for key in ("permission_mode", "sandbox_mode", "approval_policy")
            if key in data
        }
        if not legacy or "authorization" in data:
            return data
        values = dict(data)
        for key in legacy:
            values.pop(key, None)
        values["authorization"] = AuthorizationContext(**legacy)
        return values

    @property
    def permission_mode(self) -> str:
        return self.authorization.permission_mode

    @property
    def sandbox_mode(self) -> str:
        return self.authorization.sandbox_mode

    @property
    def approval_policy(self) -> str:
        return self.authorization.approval_policy

    def has_approved_tool_risk(
        self,
        tool_name: str,
        pattern: str,
        *,
        risk_level: str | None = None,
    ) -> bool:
        return any(
            getattr(risk, "tool_name", None) == tool_name
            and getattr(risk, "pattern", None) == pattern
            and getattr(risk, "risk_level", "") != "blocked"
            and (risk_level is None or getattr(risk, "risk_level", "") == risk_level)
            for risk in self.approved_tool_risks
        )


__all__ = ["ToolExecutionContext"]
