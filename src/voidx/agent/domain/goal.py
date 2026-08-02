"""Domain contracts for runtime-backed autonomous goal execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.tool_view import BoundToolView
from voidx.runtime.goal import GoalSpec


GOAL_ITERATION_USER_TEXT = "Start the autonomous goal attempt."
GOAL_PROFILE = RuntimeProfile(profile_id="goal", revision=1, name="Goal", protocol="goal")


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
