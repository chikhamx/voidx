"""Goal intake — turn the first user message into a confirmed GoalSpec."""
from __future__ import annotations

from voidx.agent.domain.goal import GOAL_PROFILE, GoalToolView
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.goal.intake_controller import GoalIntakeController
from voidx.agent.runtime.contracts import TurnRequest

_INTAKE_TOOL_IDS = frozenset(
    {
        "read",
        "find",
        "search",
        "lsp",
        "document",
        "clarify",
        "goal",
    }
)

_INTAKE_PROMPT = """\
User request:
{user_input}

---

You are the Goal Intake stage of an autonomous Goal. Convert the user request above into
a GoalSpec. You are NOT here to execute the request.

Hard rules:
- NEVER perform the task itself: do not write code, do not run commands, do not produce
  the analysis/answer the request asks for. Doing the work is a failure of this stage.
- Your only two outcomes are: call clarify with one targeted question, or call
  goal with op="init" carrying a complete spec.
- Do not emit the spec as JSON text; the goal tool call is the only submission path.
- Do not call goal with op="decision" during intake.
- goal(op="init") presents the spec to the user for approval; the user may approve,
  request revisions, or cancel. On revision feedback, update the spec and submit again.

Intake workflow:
1. Extract the objective: one stable sentence describing what must be accomplished.
2. Define the acceptance_condition: a concrete, verifiable done condition.
3. Capture the achievement_method: user-provided approach, constraints, schedule, cadence,
   priority, or execution guidance. If none is supplied, use "".
4. Resolve attempt budget: set max_attempts only when the user gives one; otherwise use 20.
5. If a required item is unclear, call clarify with exactly one question about it and wait
   for the answer. Ask about one missing item at a time; do not bundle decisions.
6. When the request is clear enough to run autonomously, call goal with op="init".
   If the user already provided enough detail, do not ask extra questions.
- Read project files only when needed to ground the spec wording, never to start the work.

Required goal(op="init") fields:
- objective: one sentence describing what must be accomplished.
- acceptance_condition: a concrete, verifiable condition that determines done.
- achievement_method: optional approach, schedule/cadence, constraints, or execution guidance; use "" if unknown.
- max_attempts: optional attempt budget; use 20 unless the user specifies otherwise.
"""


class GoalIntakeError(RuntimeError):
    """Goal spec could not be initialized from the intake turn."""


class GoalIntakeService:
    """Collect a confirmed GoalSpec from the first user message."""

    def __init__(self, runtime, goal_service) -> None:
        self._runtime = runtime
        self._goal_service = goal_service

    async def run(self, user_input: str, parent_thread_id: str | None, *, workspace: str = "") -> object:
        thread = AgentThread(
            thread_id=f"goal-intake:{parent_thread_id or 'host'}",
            session_id=parent_thread_id or "",
            workspace=workspace,
        )
        controller = GoalIntakeController()
        context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id,
            runtime_profile=GOAL_PROFILE,
            workspace=thread.workspace,
            tool_policy=GoalToolView.default(phase="intake").bind(_INTAKE_TOOL_IDS),
            goal_intake_controller=controller,
            goal_phase="intake",
        )
        await self._runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=_INTAKE_PROMPT.format(user_input=user_input),
                context=context,
                display_text=user_input,
                runtime=None,
                persist_user_input=False,
            )
        )
        spec = controller.final_spec()
        if spec is None:
            if controller.cancelled:
                raise GoalIntakeError("Goal intake cancelled; no goal was started.")
            raise GoalIntakeError(
                "Could not initialize a complete goal spec because intake did not receive "
                "goal(op=\"init\"). Use /goal <objective> --accept <condition> to start explicitly."
            )
        return await self._goal_service.start(parent_thread_id, spec)
