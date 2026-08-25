"""Tool-capable evaluator phase for autonomous Goal attempts."""

from __future__ import annotations

from voidx.agent.application.runtime.contracts import TurnRequest
from voidx.agent.domain.automation.goal import WorkCheckpoint
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext


class GoalEvaluator:
    def build_request(
        self,
        *,
        thread: AgentThread,
        context: TurnExecutionContext,
        prompt: str,
        checkpoint: WorkCheckpoint,
        guidance: tuple[dict, ...] = (),
    ) -> TurnRequest:
        evaluator_session_id = str(context.goal_evaluator_session_id or "").strip()
        if not evaluator_session_id:
            raise ValueError("Goal evaluator session binding is missing")
        evaluator_thread = AgentThread(
            thread_id=f"{thread.thread_id}:evaluator",
            session_id=evaluator_session_id,
            workspace=thread.workspace,
        )
        evaluator_context = context.model_copy(
            update={
                "thread_id": evaluator_thread.thread_id,
                "session_id": evaluator_session_id,
                "detached": False,
            }
        )
        return TurnRequest(
            thread=evaluator_thread,
            user_text=_evaluator_user_text(prompt, checkpoint),
            display_text="[goal:evaluator] verify acceptance condition",
            context=evaluator_context,
            runtime=None,
            persist_user_input=False,
            guidance=guidance,
        )


def _evaluator_user_text(prompt: str, checkpoint: WorkCheckpoint) -> str:
    lines = [
        prompt,
        "",
        "## Durable WorkCheckpoint",
        f"Summary: {checkpoint.summary}",
        f"Progress: {checkpoint.progress}",
    ]
    if checkpoint.evidence:
        lines.append("Evidence:\n" + "\n".join(f"- {item}" for item in checkpoint.evidence))
    if checkpoint.changed_files:
        lines.append(
            "Changed files:\n" + "\n".join(f"- {item}" for item in checkpoint.changed_files)
        )
    if checkpoint.verification:
        lines.append(
            "Verification:\n" + "\n".join(f"- {item}" for item in checkpoint.verification)
        )
    if checkpoint.next_hint:
        lines.append(f"Next hint: {checkpoint.next_hint}")
    return "\n".join(lines)


__all__ = ["GoalEvaluator"]
