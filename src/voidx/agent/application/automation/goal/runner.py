"""Runtime runner for one durable Goal protocol phase."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.agent.application.automation.goal.checkpoint_controller import (
    GoalCheckpointController,
)
from voidx.agent.application.automation.goal.controller import GoalController
from voidx.agent.application.automation.goal.evaluator import GoalEvaluator
from voidx.agent.application.profile_tool_policy import profile_tool_policy_for
from voidx.agent.application.runtime.contracts import GoalPhaseResult, TurnRequest
from voidx.agent.domain.agent_profile import ResolvedAgentProfile
from voidx.agent.domain.automation.goal import (
    GOAL_ITERATION_USER_TEXT,
    GoalProtocolRecord,
    GoalSpec,
    GoalState,
    GoalToolView,
    WorkCheckpoint,
)
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext


@dataclass(frozen=True)
class GoalRuntimeRunner:
    runtime: object
    evaluator: object | None = None
    store: object | None = None

    async def run_turn(
        self, *, thread, profile: ResolvedAgentProfile, input_frame: dict
    ) -> GoalPhaseResult:
        phase = str(input_frame.get("phase") or "").strip()
        if phase not in {"work", "evaluator"}:
            return GoalPhaseResult(
                phase="work",
                attempt_number=0,
                needs_resume=True,
                reason="invalid_goal_phase",
            )
        try:
            spec = GoalSpec.model_validate(input_frame.get("spec") or {})
            state = GoalState.model_validate(input_frame.get("goal_state") or {})
            attempt_number = int(input_frame.get("attempt_number") or 0)
        except Exception:
            return GoalPhaseResult(
                phase=phase,
                attempt_number=0,
                needs_resume=True,
                reason="missing_goal_state",
            )
        expected_attempt = state.attempt_count + 1
        if attempt_number != expected_attempt:
            return GoalPhaseResult(
                phase=phase,
                attempt_number=max(attempt_number, 0),
                needs_resume=True,
                reason="goal_attempt_mismatch",
            )
        if phase == "work" and attempt_number > state.max_attempts:
            return GoalPhaseResult(
                phase=phase,
                attempt_number=attempt_number,
                needs_resume=True,
                reason="max_attempts_exceeded",
            )

        context = _phase_context(
            thread=thread,
            profile=profile,
            spec=spec,
            state=state,
            phase=phase,
            attempt_number=attempt_number,
            input_frame=input_frame,
            store=self.store,
        )
        if phase == "work":
            return await self._run_work(
                thread=thread,
                spec=spec,
                state=state,
                attempt_number=attempt_number,
                input_frame=input_frame,
                context=context,
            )
        return await self._run_evaluator(
            thread=thread,
            spec=spec,
            state=state,
            attempt_number=attempt_number,
            input_frame=input_frame,
            context=context,
        )

    async def _run_work(
        self,
        *,
        thread,
        spec: GoalSpec,
        state: GoalState,
        attempt_number: int,
        input_frame: dict,
        context: TurnExecutionContext,
    ) -> GoalPhaseResult:
        work_session_id = state.work_session_id or thread.session_id or ""
        work_thread = thread.model_copy(update={"session_id": work_session_id})
        result = await self.runtime.run_turn(
            TurnRequest(
                thread=work_thread,
                user_text=_prompt_for_attempt(spec, state, attempt_number),
                display_text=f"[goal:work] {spec.objective_summary()}",
                context=context.model_copy(
                    update={"thread_id": work_thread.thread_id, "session_id": work_session_id}
                ),
                runtime=None,
                persist_user_input=False,
                guidance=tuple(input_frame.get("guidance") or ()),
            )
        )
        if getattr(result, "stop_signal", ""):
            return GoalPhaseResult(
                phase="work",
                attempt_number=attempt_number,
                needs_resume=True,
                reason=str(result.stop_signal),
            )
        controller = context.goal_checkpoint_controller
        protocol_id = controller.final_protocol_id() if controller is not None else ""
        observations = tuple(
            getattr(result, "current_turn_tool_result_summaries", ()) or ()
        )
        if (
            not protocol_id
            and observations
            and self.store is not None
            and controller is not None
            and context.goal_attempt_id
            and context.goal_lease_owner
            and context.goal_fencing_token > 0
        ):
            checkpoint = WorkCheckpoint(
                generation=state.generation,
                attempt_number=attempt_number,
                source="runtime_fallback",
                completeness="incomplete",
                summary="Current work turn produced tool observations without a model checkpoint.",
                progress="none",
                work_turn_id=context.goal_turn_id,
                observed_assistant_summary=str(
                    getattr(result, "final_assistant_summary", "") or ""
                ),
                observed_tool_result_summaries=observations,
            )
            record = GoalProtocolRecord.submitted(
                protocol_id=f"goal-fallback-{context.goal_attempt_id}",
                parent_session_id=context.goal_parent_session_id,
                generation=state.generation,
                phase="checkpoint",
                attempt_number=attempt_number,
                turn_id=context.goal_turn_id,
                session_id=work_session_id,
                payload=checkpoint,
            )
            stored = await self.store.submit_goal_protocol(
                record,
                attempt_id=context.goal_attempt_id,
                lease_owner=context.goal_lease_owner,
                fencing_token=context.goal_fencing_token,
            )
            await controller.submit_checkpoint(
                checkpoint,
                protocol_id=stored.protocol_id,
            )
            protocol_id = controller.final_protocol_id()
        if not protocol_id:
            return GoalPhaseResult(
                phase="work",
                attempt_number=attempt_number,
                needs_resume=True,
                reason="missing_work_checkpoint",
            )
        return GoalPhaseResult(
            phase="work",
            attempt_number=attempt_number,
            protocol_id=protocol_id,
        )

    async def _run_evaluator(
        self,
        *,
        thread,
        spec: GoalSpec,
        state: GoalState,
        attempt_number: int,
        input_frame: dict,
        context: TurnExecutionContext,
    ) -> GoalPhaseResult:
        try:
            checkpoint = WorkCheckpoint.model_validate(input_frame.get("checkpoint") or {})
        except Exception:
            return GoalPhaseResult(
                phase="evaluator",
                attempt_number=attempt_number,
                needs_resume=True,
                reason="missing_work_checkpoint",
            )
        if (
            checkpoint.generation != state.generation
            or checkpoint.attempt_number != attempt_number
        ):
            return GoalPhaseResult(
                phase="evaluator",
                attempt_number=attempt_number,
                needs_resume=True,
                reason="checkpoint_binding_mismatch",
            )
        evaluator = self.evaluator or GoalEvaluator()
        request = evaluator.build_request(
            thread=thread,
            context=context,
            prompt=_evaluator_prompt(spec, attempt_number),
            checkpoint=checkpoint,
            guidance=tuple(input_frame.get("guidance") or ()),
        )
        await self.runtime.run_turn(request)
        controller = context.goal_controller
        protocol_id = controller.final_protocol_id() if controller is not None else ""
        if not protocol_id:
            return GoalPhaseResult(
                phase="evaluator",
                attempt_number=attempt_number,
                needs_resume=True,
                reason="missing_goal_decision",
            )
        return GoalPhaseResult(
            phase="evaluator",
            attempt_number=attempt_number,
            protocol_id=protocol_id,
        )


def _phase_context(
    *,
    thread,
    profile: ResolvedAgentProfile,
    spec: GoalSpec,
    state: GoalState,
    phase: str,
    attempt_number: int,
    input_frame: dict,
    store: object | None,
) -> TurnExecutionContext:
    checkpoint_controller = GoalCheckpointController(
        attempt_id=str(input_frame.get("attempt_id") or "")
    )
    decision_controller = GoalController(
        attempt_id=str(input_frame.get("attempt_id") or "")
    )
    baseline = GoalToolView.default(
        workflow_enabled=spec.workflow_enabled,
        phase=phase,
    ).bind(_available_goal_tool_ids())
    session_id = (
        state.work_session_id if phase == "work" else state.evaluator_session_id
    ) or thread.session_id or ""
    return TurnExecutionContext(
        thread_id=thread.thread_id,
        session_id=session_id,
        runtime_profile=profile.runtime_profile,
        workflow_context=profile.workflow_context,
        workspace=thread.workspace,
        tool_policy=profile_tool_policy_for(profile, baseline=baseline, phase=phase),
        goal_controller=decision_controller,
        goal_checkpoint_controller=checkpoint_controller,
        goal_phase=phase,
        goal_store=store,
        goal_generation=state.generation,
        goal_parent_session_id=state.main_session_id or thread.session_id or "",
        goal_main_session_id=state.main_session_id or thread.session_id or "",
        goal_work_session_id=state.work_session_id or thread.session_id or "",
        goal_evaluator_session_id=state.evaluator_session_id,
        goal_turn_id=str(
            input_frame.get("turn_id")
            or f"{thread.thread_id}:{phase}:{attempt_number}"
        ),
        goal_attempt_number=attempt_number,
        goal_attempt_id=str(input_frame.get("attempt_id") or ""),
        goal_lease_owner=str(input_frame.get("lease_owner") or ""),
        goal_fencing_token=int(input_frame.get("fencing_token") or 0),
    )


def _evaluator_prompt(spec: GoalSpec, attempt_number: int) -> str:
    return (
        "Evaluate the Goal acceptance condition for this attempt.\n"
        f"Objective: {spec.objective}\n"
        f"Acceptance condition: {spec.acceptance_condition}\n"
        f"Attempt: {attempt_number}/{spec.max_attempts}\n"
        "Use policy-approved verification tools when needed, then call goal_decision with "
        'status="finished", "continue", or "blocked".'
    )


def _prompt_for_attempt(spec: GoalSpec, state: GoalState, attempt_number: int) -> str:
    if attempt_number <= 1:
        method = spec.achievement_method or (
            "Use the safest direct method that satisfies the acceptance condition."
        )
        return (
            f"{GOAL_ITERATION_USER_TEXT}\n\n"
            f"Objective:\n{spec.objective}\n\n"
            f"Acceptance condition:\n{spec.acceptance_condition}\n\n"
            f"Method:\n{method}\n\n"
            "Work only on actions that directly advance this objective. "
            "Before finishing, collect concrete evidence and call goal_checkpoint."
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
        "goal_init",
        "goal_checkpoint",
        "goal_decision",
        "todo",
    }


__all__ = ["GoalRuntimeRunner"]
