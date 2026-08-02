"""Runtime contracts for autonomous goal execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GoalSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective: str
    acceptance_condition: str
    achievement_method: str = ""
    max_attempts: int = Field(default=20, ge=1, le=200)
    workflow_enabled: bool = False
    generation: str = "active"

    @field_validator("objective", "acceptance_condition", "generation")
    @classmethod
    def require_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("achievement_method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.strip()

    def goal_thread_id(self, parent_thread_id: str | None) -> str:
        parent = (parent_thread_id or "default").strip() or "default"
        return f"goal:{parent}:{self.generation}"

    def goal_session_id(self, parent_thread_id: str | None) -> str:
        return self.goal_thread_id(parent_thread_id)

    def objective_summary(self) -> str:
        return self.objective.replace("\n", " ")[:80]
