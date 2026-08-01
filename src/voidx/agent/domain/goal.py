"""Domain contracts for runtime-backed autonomous goal execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.tool_view import BoundToolView


GOAL_ITERATION_USER_TEXT = "Start the autonomous goal attempt."
GOAL_PROFILE = RuntimeProfile(profile_id="goal", revision=1, name="Goal", protocol="goal")


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


class GoalState(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    objective: str
    acceptance_condition: str
    achievement_method: str = ""
    max_attempts: int = Field(ge=1, le=200)
    attempt_count: int = Field(default=0, ge=0)
    evaluator_failure_count: int = Field(default=0, ge=0)
    last_progress_key: str = ""
    repeated_progress_count: int = Field(default=0, ge=0)
    last_evaluator_summary: str = ""
    last_evaluator_next_hint: str = ""
    last_evaluator_missing: tuple[str, ...] = ()
    blocked_reason: str = ""
    active: bool = True

    @classmethod
    def from_spec(cls, spec: GoalSpec, *, run_id: str) -> "GoalState":
        return cls(
            run_id=run_id,
            objective=spec.objective,
            acceptance_condition=spec.acceptance_condition,
            achievement_method=spec.achievement_method,
            max_attempts=spec.max_attempts,
        )


class GoalToolView(BoundToolView):
    workflow_enabled: bool = False
    phase: str = "work"

    @classmethod
    def default(cls, *, workflow_enabled: bool = False, phase: str = "work") -> "GoalToolView":
        return cls(workflow_enabled=workflow_enabled, phase=phase)

    def bind(self, available_tool_ids: set[str] | list[str] | tuple[str, ...]) -> "GoalToolView":
        allowed = {
            "read",
            "find",
            "search",
            "lsp",
            "document",
            "websearch",
            "webfetch",
            "mcp",
            "skill",
            "task_status",
        }
        if self.phase == "work":
            allowed.update({"bash", "write", "replace", "manage", "lsp_format"})
        elif self.phase == "intake":
            allowed.update({"clarify", "goal"})
        elif self.phase == "evaluator":
            allowed.add("goal")
        if self.workflow_enabled:
            allowed.update({"workflow", "todo"})
        return self.model_copy(update={"bound_tool_ids": frozenset(set(available_tool_ids) & allowed)})
