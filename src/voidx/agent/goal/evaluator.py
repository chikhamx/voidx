"""Tool-capable evaluator phase for autonomous Goal attempts."""

from __future__ import annotations

from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.runtime.contracts import TurnRequest


class GoalEvaluator:
    """Runs the evaluator as a normal runtime turn with Goal protocol tools."""

    async def run_phase(
        self,
        *,
        runtime,
        thread,
        context: TurnExecutionContext,
        prompt: str,
        controller,
        work_result,
    ) -> None:
        del controller, work_result
        run_turn = getattr(runtime, "run_turn")
        await run_turn(
            TurnRequest(
                thread=thread,
                user_text=prompt,
                display_text="[goal:evaluator] verify acceptance condition",
                context=context,
                runtime=None,
                persist_user_input=False,
            )
        )
