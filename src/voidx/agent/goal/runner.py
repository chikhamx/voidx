"""Runtime runner for one autonomous Goal attempt."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.agent.domain.goal import GOAL_ITERATION_USER_TEXT, GOAL_PROFILE, GoalSpec, GoalState, GoalToolView
from voidx.agent.domain.thread import DecisionMetadata, RuntimeDecision
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.goal.controller import GoalController
from voidx.agent.goal.evaluator import GoalEvaluator
from voidx.agent.runtime.contracts import TurnRequest


_DEFAULT_CONTINUE_DELAY_SECONDS = 0.0


@dataclass(frozen=True)
class GoalRuntimeRunner:
    runtime: object
    evaluator: object

    async def run_turn(self, *, thread, profile, input_frame: dict) -> RuntimeDecision:
        del profile
        try:
            spec = GoalSpec.model_validate(input_frame.get("spec") or {})
            state = GoalState.model_validate(input_frame.get("goal_state") or {})
        except Exception:
            return RuntimeDecision(
                outcome="failed",
                summary="Goal input frame was missing required state.",
                reason="missing_goal_state",
            )

        attempt_index = state.attempt_count + 1
        if attempt_index > state.max_attempts:
            return _decision_with_patch(
                RuntimeDecision(
                    outcome="blocked",
                    summary="Goal reached its maximum number of attempts.",
                    reason="max_attempts_exceeded",
                ),
                state,
                attempt_index=state.attempt_count,
                active=False,
                blocked_reason="max_attempts_exceeded",
            )
        controller = GoalController(attempt_id=f"{thread.thread_id}:{attempt_index}")
        work_context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            runtime_profile=GOAL_PROFILE,
            workspace=thread.workspace,
            tool_policy=GoalToolView.default(workflow_enabled=spec.workflow_enabled, phase="work").bind(
                _available_goal_tool_ids()
            ),
            goal_controller=controller,
            goal_phase="work",
        )
        result = await self.runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=_prompt_for_attempt(spec, state, attempt_index),
                display_text=f"[goal] {spec.objective_summary()}",
                context=work_context,
                runtime=None,
                persist_user_input=False,
            )
        )
        if getattr(result, "stop_signal", ""):
            return _decision_with_patch(
                RuntimeDecision(
                    outcome="needs_user",
                    summary=f"Goal attempt stopped: {result.stop_signal}",
                    reason=str(result.stop_signal),
                ),
                state,
                attempt_index=attempt_index,
                active=True,
            )

        evaluator_context = work_context.model_copy(
            update={
                "goal_phase": "evaluator",
                "tool_policy": GoalToolView.default(
                    workflow_enabled=spec.workflow_enabled, phase="evaluator"
                ).bind(_available_goal_tool_ids()),
            }
        )
        await self.evaluator.run_phase(
            runtime=self.runtime,
            thread=thread,
            context=evaluator_context,
            prompt=_evaluator_prompt(spec, state, attempt_index),
            controller=controller,
            work_result=result,
        )
        decision = controller.final_decision()
        if decision is None:
            return _decision_with_patch(
                RuntimeDecision(
                    outcome="blocked",
                    summary="Evaluator did not submit a goal decision.",
                    reason="missing_goal_decision",
                ),
                state,
                attempt_index=attempt_index,
                active=False,
                blocked_reason="missing_goal_decision",
            )
        return _decision_from_controller(state, decision, attempt_index=attempt_index)


def _evaluator_prompt(spec: GoalSpec, state: GoalState, attempt_index: int) -> str:
    return (
        "Evaluate the Goal acceptance condition for this attempt.\n"
        f"Objective: {spec.objective}\n"
        f"Acceptance condition: {spec.acceptance_condition}\n"
        f"Attempt: {attempt_index}/{spec.max_attempts}\n"
        "Use policy-approved verification tools when needed, then call goal with "
        "op=\"decision\" and status=\"finished\", \"continue\", or \"blocked\"."
    )


def _decision_from_controller(
    state: GoalState, decision: RuntimeDecision, *, attempt_index: int
) -> RuntimeDecision:
    active = decision.outcome == "continue"
    blocked_reason = decision.reason if decision.outcome == "blocked" else ""
    return _decision_with_patch(
        decision,
        state,
        attempt_index=attempt_index,
        active=active,
        blocked_reason=blocked_reason,
    )




def _decision_with_patch(
    decision: RuntimeDecision,
    state: GoalState,
    *,
    attempt_index: int,
    active: bool,
    blocked_reason: str = "",
) -> RuntimeDecision:
    progress_key = decision.reason or state.last_progress_key
    repeated = (
        state.repeated_progress_count + 1
        if progress_key and progress_key == state.last_progress_key
        else 0
    )
    patch = {
        "attempt_count": attempt_index,
        "evaluator_failure_count": 0,
        "last_progress_key": progress_key,
        "repeated_progress_count": repeated,
        "last_evaluator_summary": decision.summary,
        "last_evaluator_next_hint": "",
        "last_evaluator_missing": state.last_evaluator_missing,
        "blocked_reason": blocked_reason,
        "active": active,
    }
    return decision.model_copy(update={"metadata": DecisionMetadata(goal_state_patch=patch)})


def _prompt_for_attempt(spec: GoalSpec, state: GoalState, attempt_index: int) -> str:
    if attempt_index <= 1:
        method = spec.achievement_method or (
            "Use the safest direct method that satisfies the acceptance condition."
        )
        return (
            f"{GOAL_ITERATION_USER_TEXT}\n\n"
            f"Objective:\n{spec.objective}\n\n"
            f"Acceptance condition:\n{spec.acceptance_condition}\n\n"
            f"Method:\n{method}\n\n"
            "Work only on actions that directly advance this objective. "
            "Do not inspect or modify the Goal Runtime implementation unless the objective "
            "explicitly asks for it. "
            "Before finishing, collect concrete evidence for the acceptance condition."
        )
    return (
        "Continue the autonomous goal attempt.\n\n"
        f"Objective:\n{spec.objective}\n\n"
        f"Acceptance condition:\n{spec.acceptance_condition}\n\n"
        f"Previous evaluator summary:\n{state.last_evaluator_summary or 'none'}\n\n"
        f"Suggested next step:\n{state.last_evaluator_next_hint or 'none'}"
    )


def _available_goal_tool_ids() -> set[str]:
    return {
        "read",
        "find",
        "search",
        "lsp",
        "document",
        "bash",
        "write",
        "replace",
        "manage",
        "lsp_format",
        "websearch",
        "webfetch",
        "mcp",
        "skill",
        "goal",
        "workflow",
        "todo",
    }
