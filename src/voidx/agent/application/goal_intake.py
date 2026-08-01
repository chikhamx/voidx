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
        "websearch",
        "webfetch",
        "mcp",
        "clarify",
        "goal",
    }
)

_INTAKE_PROMPT = """\
You are initializing an autonomous Goal from the user's first request.

Rules:
- If the objective or acceptance condition is unclear, call clarify with one targeted question.
- When both are clear, call goal with op="init" and the complete goal spec.
- Do not emit the spec as JSON text; the goal tool call is the only successful submission path.
- Do not call goal with op="decision" during intake.
- Use achievement_method for important execution guidance from the user; otherwise use "".
- Read project files only when needed to ground the goal spec.

Required goal(op="init") fields:
- objective: one sentence describing what must be accomplished.
- acceptance_condition: a concrete, verifiable condition that determines done.
- achievement_method: optional approach or execution guidance; use "" if unknown.
- max_attempts: optional attempt budget; use 20 unless the user specifies otherwise.

User request:
{user_input}
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
            raise GoalIntakeError(
                "Could not initialize a complete goal spec because intake did not receive "
                "goal(op=\"init\"). Use /goal <objective> --accept <condition> to start explicitly."
            )
        return await self._goal_service.start(parent_thread_id, spec)
