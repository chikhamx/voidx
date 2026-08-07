"""Goal conversational mode — in-session idle turns for goal-profile sessions.

When a goal-profile session has no active goal, the user's message runs as an
in-session goal-profile turn (idle phase). The turn is conversational: it uses
read-only tools plus clarify and goal. If the model submits a GoalSpec via
goal(op="init") and the user approves, the autonomous goal loop starts; the
session then stays in goal mode and can chat or init again after it ends.
"""
from __future__ import annotations

from voidx.agent.domain.automation.goal import GOAL_PROFILE, GoalToolView
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.application.automation.goal.intake_controller import GoalIntakeController
from voidx.agent.application.runtime.contracts import TurnRequest


class GoalIdleTurnService:
    """Run one conversational goal-profile turn in the host session.

    Returns the started goal status when the turn produced an approved GoalSpec,
    otherwise ``None`` (the turn was pure conversation).
    """

    def __init__(self, runtime, goal_service) -> None:
        self._runtime = runtime
        self._goal_service = goal_service

    async def run(self, user_input: str, thread, *, parent_thread_id: str | None) -> object | None:
        controller = GoalIntakeController()
        context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            runtime_profile=GOAL_PROFILE,
            workspace=getattr(thread, "workspace", "") or "",
            tool_policy=GoalToolView.default(phase="idle").bind(_available_idle_tool_ids()),
            goal_intake_controller=controller,
            goal_phase="idle",
        )
        await self._runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=user_input,
                display_text=user_input,
                context=context,
                runtime=None,
                persist_user_input=False,
            )
        )
        spec = controller.final_spec()
        if spec is None or controller.cancelled:
            return None
        return await self._goal_service.start(parent_thread_id, spec)


def _available_idle_tool_ids() -> set[str]:
    return {
        "read",
        "find",
        "search",
        "lsp",
        "document",
        "clarify",
        "goal",
    }
