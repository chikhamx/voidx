"""Tool-capable evaluator phase for autonomous Goal attempts."""

from __future__ import annotations

from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.runtime.contracts import TurnRequest, TurnResult


class GoalEvaluator:
    """Runs the evaluator as an independent runtime turn with Goal protocol tools.

    The evaluator runs with a detached thread (no session_id) so it never loads
    the work-phase conversation history; it judges from the structured evidence
    carried by the work turn's TurnResult instead.
    """

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
        del controller
        evaluator_thread = AgentThread(
            thread_id=f"{thread.thread_id}:evaluator",
            session_id=None,
            workspace=thread.workspace,
        )
        evaluator_context = context.model_copy(
            update={"thread_id": evaluator_thread.thread_id, "session_id": ""}
        )
        run_turn = getattr(runtime, "run_turn")
        await run_turn(
            TurnRequest(
                thread=evaluator_thread,
                user_text=_evaluator_user_text(prompt, work_result),
                display_text="[goal:evaluator] verify acceptance condition",
                context=evaluator_context,
                runtime=None,
                persist_user_input=False,
            )
        )


def _evaluator_user_text(prompt: str, work_result) -> str:
    evidence = _format_work_evidence(work_result)
    if not evidence:
        return prompt
    return f"{prompt}\n\n## Work phase evidence\n{evidence}"


def _format_work_evidence(work_result) -> str:
    if work_result is None or not isinstance(work_result, TurnResult):
        return ""
    parts: list[str] = []
    summary = (work_result.final_assistant_summary or "").strip()
    if summary:
        parts.append(f"Assistant summary:\n{summary}")
    tool_summaries = [item for item in work_result.tool_result_summaries if str(item).strip()]
    if tool_summaries:
        parts.append("Tool results:\n" + "\n".join(f"- {item}" for item in tool_summaries))
    return "\n\n".join(parts)
