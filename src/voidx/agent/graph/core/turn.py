from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from voidx.agent.graph.core.loop import LlmLoopState
from voidx.runtime.intent import InteractionMode
from voidx.agent.graph.streaming import extract_text
from voidx.agent.graph.topology import latest_user_text
from voidx.agent.graph.turn_control import (
    INVALID_TURN_PROMPT,
    NO_USER_RESPONSE_PROMPT,
    TURN_START_PROMPT,
    TURN_STOP_PROMPT,
    TurnClassification,
    classify_turn_call,
    normalize_terminal_message,
    validate_turn_call,
)
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    TaskState,
    goal_label,
)
from voidx.agent.runtime_context import TaskIntent
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.workflow.service import reconcile_workflow_runs_for_turn


@dataclass
class TurnControlResult:
    action: Literal["retry", "break", "fail", "fallthrough"]
    llm_messages: list[BaseMessage]
    context_tokens: int
    turn_state: str
    runtime_task_state: TaskState
    failure_msg: AIMessage | None = None


async def handle_turn_control_response(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
    state_messages: list[BaseMessage],
    interaction_mode_value: str,
    estimate_tokens: Any,
    rerender_task_context: Any,
) -> TurnControlResult:
    classification = classify_turn_call(assistant_msg)
    has_text = bool(extract_text(assistant_msg).strip())
    if (
        loop.turn_prompt_active
        and has_text
        and classification != TurnClassification.PLAIN_TEXT
        and not (
            classification == TurnClassification.VALID_TURN
            and loop.pending_provisional is None
        )
    ):
        classification = TurnClassification.INVALID_TURN

    if classification == TurnClassification.VALID_START:
        start_call = assistant_msg.tool_calls[0]
        tool_call_id = str(start_call.get("id") or "")
        if turn_state != "initial":
            llm_messages = [
                *llm_messages,
                assistant_msg,
                ToolMessage(
                    content="Goal already declared.",
                    tool_call_id=tool_call_id,
                    name="turn",
                ),
            ]
            loop.context_tokens = estimate_tokens(llm_messages)
            return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)

        start_args = start_call.get("args") or {}
        intent_value = str(start_args.get("intent") or "coding")
        goal_text = str(start_args.get("goal") or "").strip()
        resolution = GoalResolution(
            intent=IntentResolution(
                type=TaskIntent.GENERAL if intent_value == "general" else TaskIntent.CODING,
            ),
            goal=GoalSpec(desc=goal_text),
            plan=None,
        )
        runtime_task_state.update_after_turn(
            resolution,
            latest_user_text(state_messages),
        )
        reconciled_workflow_runs = reconcile_workflow_runs_for_turn(
            goal_resolution=resolution,
            after_state=runtime_task_state,
        )
        runtime_task_state.workflow_runs = {
            run.name: run for run in reconciled_workflow_runs
        }
        graph._task_state = runtime_task_state.model_copy(deep=True)
        graph._invalidate_tui_for_turn()
        turn_state = "running"
        loop.turn_prompt_active = False
        llm_messages = rerender_task_context(llm_messages, "running", runtime_task_state)
        llm_messages = [
            *llm_messages,
            assistant_msg,
            ToolMessage(
                content=(
                    f"Turn started: {goal_label(runtime_task_state.current_goal) or goal_text}. "
                    f"Intent: {intent_value}. Continue with the next appropriate tool or workflow step. "
                    "When the user-facing response is complete, call turn operation='stop'."
                ),
                tool_call_id=tool_call_id,
                name="turn",
            ),
        ]
        loop.context_tokens = estimate_tokens(llm_messages)
        return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)

    if classification == TurnClassification.VALID_TURN:
        if loop.pending_provisional is not None:
            turn_terminal = loop.pending_provisional
            turn_terminal_visible = loop.pending_provisional_visible
        else:
            turn_terminal = assistant_msg if has_text else loop.pending_provisional
            turn_terminal_visible = not loop.turn_prompt_active
        if validate_turn_call(assistant_msg, turn_terminal):
            graph._turn_metrics.increment("turn_control_called")
            loop.terminal_msg = normalize_terminal_message(turn_terminal)
            loop.terminal_msg_visible = turn_terminal_visible
            turn_state = "committed"
            return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
        graph._turn_metrics.increment("turn_control_invalid")
        if not loop.invalid_turn_repaired:
            loop.invalid_turn_repaired = True
            loop.turn_prompt_active = True
            no_response = turn_terminal is None or not bool(
                extract_text(turn_terminal).strip()
            )
            repair_prompt = NO_USER_RESPONSE_PROMPT if no_response else INVALID_TURN_PROMPT
            llm_messages = [
                *llm_messages,
                HumanMessage(
                    content=repair_prompt,
                    additional_kwargs={GUIDANCE_MARKER: True},
                ),
            ]
            loop.context_tokens = estimate_tokens(llm_messages)
            return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
        failure_msg = AIMessage(
            content="LLM call failed: model repeatedly returned an invalid turn control call."
        )
        return TurnControlResult("fail", llm_messages, loop.context_tokens, turn_state, runtime_task_state, failure_msg)

    if classification == TurnClassification.REGULAR_TOOLS:
        if loop.turn_prompt_active:
            graph._turn_metrics.increment("turn_control_prompt_succeeded")
        loop.terminal_msg = assistant_msg
        return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)

    if classification == TurnClassification.INVALID_TURN:
        if not loop.invalid_turn_repaired:
            graph._turn_metrics.increment("turn_control_mixed_tools")
            graph._turn_metrics.increment("turn_control_invalid")
            loop.invalid_turn_repaired = True
            loop.turn_prompt_active = True
            llm_messages = [
                *llm_messages,
                HumanMessage(
                    content=INVALID_TURN_PROMPT,
                    additional_kwargs={GUIDANCE_MARKER: True},
                ),
            ]
            loop.context_tokens = estimate_tokens(llm_messages)
            return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
        failure_msg = AIMessage(
            content="LLM call failed: model repeatedly returned an invalid turn control call."
        )
        return TurnControlResult("fail", llm_messages, loop.context_tokens, turn_state, runtime_task_state, failure_msg)

    if classification == TurnClassification.PLAIN_TEXT:
        if (
            turn_state == "initial"
            and loop.missing_turn_count == 0
            and not loop.start_prompt_injected
            and not loop.turn_prompt_active
            and bool(state_messages and isinstance(state_messages[-1], HumanMessage))
        ):
            loop.terminal_msg = normalize_terminal_message(assistant_msg)
            loop.terminal_msg_visible = not loop.turn_prompt_active
            turn_state = "committed"
            return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
        if loop.missing_turn_count == 0:
            loop.pending_provisional = assistant_msg
            loop.pending_provisional_visible = not loop.turn_prompt_active
        if (
            turn_state == "initial"
            and not loop.start_prompt_injected
            and loop.missing_turn_count == 0
            and interaction_mode_value not in {InteractionMode.PLAN.value, InteractionMode.GOAL.value}
        ):
            loop.start_prompt_injected = True
            loop.turn_prompt_active = True
            graph._turn_metrics.increment("turn_control_missing")
            llm_messages = [
                *llm_messages,
                assistant_msg,
                HumanMessage(
                    content=TURN_START_PROMPT,
                    additional_kwargs={GUIDANCE_MARKER: True},
                ),
            ]
            loop.context_tokens = estimate_tokens(llm_messages)
            return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
        loop.missing_turn_count += 1
        graph._turn_metrics.increment("turn_control_missing")
        if loop.missing_turn_count == 1:
            graph._turn_metrics.increment("turn_control_first_prompt")
            loop.turn_prompt_active = True
            llm_messages = [
                *llm_messages,
                assistant_msg,
                HumanMessage(
                    content=TURN_STOP_PROMPT,
                    additional_kwargs={GUIDANCE_MARKER: True},
                ),
            ]
            loop.context_tokens = estimate_tokens(llm_messages)
            return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
        graph._turn_metrics.increment("turn_control_second_prompt")
        loop.terminal_msg = normalize_terminal_message(loop.pending_provisional)
        loop.terminal_msg_visible = loop.pending_provisional_visible
        return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)

    graph._turn_metrics.increment("turn_control_prompt_succeeded")
    loop.terminal_msg = assistant_msg
    return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
