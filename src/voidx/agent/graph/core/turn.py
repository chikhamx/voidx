from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from voidx.agent.graph.core.loop import LlmLoopState
from voidx.runtime.intent import InteractionMode
from voidx.agent.graph.streaming import extract_text
from voidx.agent.graph.topology import latest_user_text
from voidx.agent.graph.turn_control import (
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
)
from voidx.agent.runtime_context import TaskIntent
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.workflow.service import reconcile_workflow_runs_for_turn


@dataclass
class TurnControlResult:
    action: Literal["retry", "break", "fail"]
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
    if _is_invalid_prompt_response(classification, has_text, loop):
        classification = TurnClassification.INVALID_TURN

    if classification == TurnClassification.VALID_START:
        return await _handle_turn_start(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            state_messages=state_messages,
            estimate_tokens=estimate_tokens,
            rerender_task_context=rerender_task_context,
        )

    if classification == TurnClassification.VALID_TURN:
        return _handle_turn_stop(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            has_text=has_text,
            estimate_tokens=estimate_tokens,
        )

    if classification == TurnClassification.REGULAR_TOOLS:
        if loop.turn_prompt_active:
            graph._turn_metrics.increment("turn_control_prompt_succeeded")
        loop.terminal_msg = assistant_msg
        return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)

    if classification == TurnClassification.INVALID_TURN:
        return _handle_invalid_turn(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            estimate_tokens=estimate_tokens,
        )

    if classification == TurnClassification.PLAIN_TEXT:
        return _handle_plain_text(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            state_messages=state_messages,
            interaction_mode_value=interaction_mode_value,
            estimate_tokens=estimate_tokens,
        )

    graph._turn_metrics.increment("turn_control_prompt_succeeded")
    loop.terminal_msg = assistant_msg
    return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)


def _is_invalid_prompt_response(
    classification: TurnClassification,
    has_text: bool,
    loop: LlmLoopState,
) -> bool:
    return (
        loop.turn_prompt_active
        and has_text
        and classification != TurnClassification.PLAIN_TEXT
        and not (
            classification == TurnClassification.VALID_TURN
            and loop.pending_provisional is None
        )
    )


async def _handle_turn_start(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
    state_messages: list[BaseMessage],
    estimate_tokens: Any,
    rerender_task_context: Any,
) -> TurnControlResult:
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
    start_params = start_args.get("params") or {}
    intent_value = str(start_params.get("intent") or "coding")
    goal_text = str(start_params.get("goal") or "").strip()
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
                "Check the active workflow in the task state and enter it if applicable, "
                "otherwise proceed with the work directly."
            ),
            tool_call_id=tool_call_id,
            name="turn",
        ),
    ]
    loop.context_tokens = estimate_tokens(llm_messages)
    return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)


def _handle_turn_stop(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
    has_text: bool,
    estimate_tokens: Any,
) -> TurnControlResult:
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
        llm_messages = [
            *llm_messages,
            assistant_msg,
            HumanMessage(
                content=NO_USER_RESPONSE_PROMPT,
                additional_kwargs={GUIDANCE_MARKER: True},
            ),
        ]
        loop.context_tokens = estimate_tokens(llm_messages)
        return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
    return _invalid_turn_failure(llm_messages, loop, turn_state, runtime_task_state)


def _handle_invalid_turn(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
    estimate_tokens: Any,
) -> TurnControlResult:
    has_text = bool(extract_text(assistant_msg).strip())
    if has_text:
        graph._turn_metrics.increment("turn_control_invalid_committed")
        if loop.pending_provisional is not None and loop.turn_prompt_active:
            terminal = loop.pending_provisional
            terminal_visible = loop.pending_provisional_visible
        else:
            terminal = assistant_msg
            terminal_visible = not loop.turn_prompt_active
        loop.terminal_msg = normalize_terminal_message(terminal)
        loop.terminal_msg_visible = terminal_visible
        turn_state = "committed"
        return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
    if not loop.invalid_turn_repaired:
        graph._turn_metrics.increment("turn_control_mixed_tools")
        graph._turn_metrics.increment("turn_control_invalid")
        loop.invalid_turn_repaired = True
        loop.turn_prompt_active = True
        llm_messages = [
            *llm_messages,
            assistant_msg,
            HumanMessage(
                content=NO_USER_RESPONSE_PROMPT,
                additional_kwargs={GUIDANCE_MARKER: True},
            ),
        ]
        loop.context_tokens = estimate_tokens(llm_messages)
        return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
    return _invalid_turn_failure(llm_messages, loop, turn_state, runtime_task_state)


def _handle_plain_text(
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
) -> TurnControlResult:
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
    text = extract_text(assistant_msg).strip()
    if loop.missing_turn_count == 1 and len(text.splitlines()) >= 3:
        graph._turn_metrics.increment("turn_control_auto_committed")
        loop.terminal_msg = normalize_terminal_message(assistant_msg)
        loop.terminal_msg_visible = loop.pending_provisional_visible
        turn_state = "committed"
        return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
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


def _invalid_turn_failure(
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
) -> TurnControlResult:
    failure_msg = AIMessage(
        content="LLM call failed: model repeatedly returned an invalid turn control call."
    )
    return TurnControlResult("fail", llm_messages, loop.context_tokens, turn_state, runtime_task_state, failure_msg)
