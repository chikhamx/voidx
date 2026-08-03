"""Domain contracts for runtime-backed autonomous goal execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.prompt_policy import GoalPromptPolicy
from voidx.agent.domain.tool_view import BoundToolView
from voidx.runtime.goal import GoalSpec


GOAL_ITERATION_USER_TEXT = "Start the autonomous goal attempt."
GOAL_PROFILE = RuntimeProfile(
    profile_id="goal", revision=1, name="Goal", protocol="goal",
    prompt_policy=GoalPromptPolicy(),
)

GOAL_INTAKE_DIRECTIVE = """\
## Goal Intake Stage

This turn is the intake stage of an autonomous Goal. Its sole responsibility is to
produce a GoalSpec from the user's request — never to execute the request itself.

- Permitted outcomes: call clarify with one targeted question, or call goal with
  op="init" and a complete spec.
- Forbidden: performing the task, producing the requested analysis/answer, writing
  code, or running commands for the task. The work phase starts only after intake.
- goal(op="init") presents the spec for user approval; on revision feedback, update
  the spec and submit again.
"""

GOAL_EVALUATOR_DIRECTIVE = """\
## Goal Evaluator Stage

This turn is the evaluator stage of an autonomous Goal. Its sole responsibility is
to judge whether the work-phase evidence satisfies the acceptance condition, then
submit exactly one lifecycle decision.

Follow this procedure:
1. Review — read the work-phase evidence in this turn's input and check each
   acceptance condition against it. The work phase already ran; never re-run the
   task, and never answer with a plain-text acceptance report.
2. Verify — spot-check any evidence that looks missing or unreliable with
   read-only tools (read, find, search, lsp, document). You have no execution
   tools; do not attempt to run commands.
3. Decide — call goal with op="decision":
   - status="finished" when every condition is backed by concrete evidence;
   - status="continue" when evidence is insufficient — name the missing evidence
     in the reason so the next work attempt collects it;
   - status="blocked" when the goal cannot proceed.
   In the reason field, cite the evidence or files you relied on. This call is
the only way the turn ends.
"""

GOAL_IDLE_DIRECTIVE = """\
## Goal Idle Stage

This turn runs in goal mode while no autonomous goal is active. You may converse
with the user, answer questions with read-only tools, and help shape the next
GoalSpec — but you never execute the task itself.

Hard rules:
- NEVER perform the work: do not write code, do not run commands, do not produce
  the requested artifact. Work happens only inside the autonomous goal loop.
- You have read-only tools plus clarify and goal; no write or shell tools.
- When the user wants a goal to run, convert the request into a GoalSpec and call
  goal with op="init". goal(op="init") presents the spec for user approval; on
  revision feedback, update the spec and submit again. On cancel, drop it.
- Do not call goal with op="decision"; that op is evaluator-only.
- Otherwise answer directly and conversationally.
"""


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
            allowed -= {"websearch", "webfetch", "mcp", "skill"}
        elif self.phase == "evaluator":
            allowed.add("goal")
            allowed -= {"websearch", "webfetch", "mcp", "skill"}
        elif self.phase == "idle":
            allowed.update({"clarify", "goal"})
            allowed -= {"websearch", "webfetch", "mcp", "skill"}
        if self.workflow_enabled:
            allowed.update({"workflow", "todo"})
        return self.model_copy(update={"bound_tool_ids": frozenset(set(available_tool_ids) & allowed)})
