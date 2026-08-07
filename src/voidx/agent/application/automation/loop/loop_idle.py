"""Loop conversational mode — in-session idle turns for loop-profile sessions.

When a loop-profile session has no active loop, the user's message runs as an
in-session loop-profile turn (idle phase). The turn is conversational: it uses
read-only tools plus clarify and loop. If the model submits a LoopSpec via
loop(op="init") and the user approves, the autonomous loop starts; the session
then stays in loop mode and can chat or init again after it ends.
"""
from __future__ import annotations

from voidx.agent.domain.automation.loop import LOOP_PROFILE, LoopToolView
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.application.automation.loop.intake_controller import LoopIntakeController
from voidx.agent.application.runtime.contracts import TurnRequest


class LoopIdleTurnService:
    """Run one conversational loop-profile turn in the host session.

    Returns the started loop status when the turn produced an approved LoopSpec,
    otherwise ``None`` (the turn was pure conversation).
    """

    def __init__(self, runtime, loop_service) -> None:
        self._runtime = runtime
        self._loop_service = loop_service

    async def run(self, user_input: str, thread, *, parent_thread_id: str | None) -> object | None:
        controller = LoopIntakeController()
        context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            runtime_profile=LOOP_PROFILE,
            workspace=getattr(thread, "workspace", "") or "",
            tool_policy=LoopToolView.default(phase="idle").bind(_available_idle_tool_ids()),
            loop_intake_controller=controller,
            loop_phase="idle",
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
        return await self._loop_service.start(parent_thread_id, spec)


def _available_idle_tool_ids() -> set[str]:
    return {
        "read",
        "find",
        "search",
        "lsp",
        "document",
        "clarify",
        "loop",
    }
