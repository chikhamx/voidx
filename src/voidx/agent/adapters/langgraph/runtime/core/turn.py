from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from voidx.agent.adapters.langgraph.runtime.core.loop import LlmLoopState
from voidx.agent.domain.task.intent import InteractionMode
from voidx.agent.adapters.langgraph.runtime.streaming import extract_text
from voidx.agent.adapters.langgraph.runtime.topology import latest_user_text
from voidx.agent.adapters.langgraph.runtime.turn_control import (
    NO_USER_RESPONSE_PROMPT,
    TURN_START_PROMPT,
    TURN_STOP_PROMPT,
    TurnClassification,
    normalize_terminal_message,
    validate_turn_call,
)
from voidx.agent.domain.task.state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    TaskState,
)
from voidx.agent.application.runtime_context import TaskIntent
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.agent.application.automation.workflow.service import reconcile_workflow_runs_for_turn
from voidx.agent.domain.automation.workflow_schema import WorkflowDAG


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
    loop_controller: Any | None = None,
    protocol: Any | None = None,
    workflow_dag: WorkflowDAG | None = None,
) -> TurnControlResult:
    from voidx.agent.adapters.langgraph.runtime.control_protocol import (
        TurnToolProtocol,
    )

    protocol = protocol or TurnToolProtocol()
    classification = protocol.classify(assistant_msg)
    has_text = bool(extract_text(assistant_msg).strip())
    if _is_invalid_prompt_response(classification, has_text, loop):
        classification = TurnClassification.INVALID_TURN

    if protocol.decision_missing(assistant_msg, loop, controller=loop_controller):
        return _prompt_for_loop_decision(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            estimate_tokens=estimate_tokens,
            repair_prompt=protocol.repair_prompt(),
        )

    if classification in {
        TurnClassification.VALID_START,
        TurnClassification.VALID_START_WITH_TOOLS,
    }:
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
            with_tools=classification == TurnClassification.VALID_START_WITH_TOOLS,
            workflow_dag=workflow_dag,
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

    if classification == TurnClassification.VALID_STOP_WITH_TOOLS:
        return _handle_turn_stop_with_tools(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
        )

    if classification == TurnClassification.REGULAR_TOOLS:
        if loop.turn_prompt_active:
            graph._turn_metrics.increment("turn_control_prompt_succeeded")
        loop.terminal_msg = _loop_commit_summary_message(
            assistant_msg,
            protocol=protocol,
            loop_controller=loop_controller,
        ) or assistant_msg
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
            protocol=protocol,
            estimate_tokens=estimate_tokens,
        )

    graph._turn_metrics.increment("turn_control_prompt_succeeded")
    loop.terminal_msg = assistant_msg
    return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)


def _loop_commit_summary_message(
    assistant_msg: AIMessage,
    *,
    protocol: Any,
    loop_controller: Any | None,
) -> AIMessage | None:
    if getattr(protocol, "protocol_id", "turn") != "loop":
        return None
    if extract_text(assistant_msg).strip() or not _has_loop_commit_call(assistant_msg):
        return None
    decision = loop_controller.final_decision() if loop_controller is not None else None
    summary = str(getattr(decision, "summary", "") or "").strip()
    if not summary:
        return None
    return AIMessage(content=summary)


def _has_loop_commit_call(assistant_msg: AIMessage) -> bool:
    for call in getattr(assistant_msg, "tool_calls", None) or []:
        if not isinstance(call, dict) or call.get("name") != "loop":
            continue
        args = call.get("args")
        if isinstance(args, dict) and args.get("operation") == "commit":
            return True
    return False


def _prompt_for_loop_decision(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
    estimate_tokens: Any,
    repair_prompt: str,
) -> TurnControlResult:
    graph._turn_metrics.increment("loop_decision_prompted")
    loop.protocol_repairs += 1
    loop.turn_prompt_active = True
    llm_messages = [
        *llm_messages,
        assistant_msg,
        HumanMessage(
            content=repair_prompt,
            additional_kwargs={GUIDANCE_MARKER: True},
        ),
    ]
    loop.context_tokens = estimate_tokens(llm_messages)
    return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)


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
    with_tools: bool = False,
    workflow_dag: WorkflowDAG | None = None,
) -> TurnControlResult:
    start_call = _turn_call_from_message(assistant_msg)
    tool_call_id = str((start_call or {}).get("id") or "")
    regular_calls = [
        call
        for call in (getattr(assistant_msg, "tool_calls", None) or [])
        if isinstance(call, dict) and str(call.get("name") or "") != "turn"
    ]

    if turn_state != "initial":
        if with_tools and regular_calls:
            loop.terminal_msg = _message_with_tool_calls(assistant_msg, regular_calls)
            return TurnControlResult(
                "break",
                llm_messages,
                loop.context_tokens,
                turn_state,
                runtime_task_state,
            )
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

    start_args = (start_call or {}).get("args") or {}
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
    if workflow_dag is not None:
        reconciled_workflow_runs = reconcile_workflow_runs_for_turn(
            goal_resolution=resolution,
            after_state=runtime_task_state,
            dag=workflow_dag,
        )
        runtime_task_state.workflow_runs = {
            run.name: run for run in reconciled_workflow_runs
        }
    graph._task_state = runtime_task_state.model_copy(deep=True)
    graph._invalidate_tui_for_turn()
    turn_state = "running"
    loop.turn_prompt_active = False
    llm_messages = rerender_task_context(llm_messages, "running", runtime_task_state)

    if with_tools and regular_calls:
        loop.terminal_msg = _message_with_tool_calls(assistant_msg, regular_calls)
        return TurnControlResult(
            "break",
            llm_messages,
            estimate_tokens(llm_messages),
            turn_state,
            runtime_task_state,
        )

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


def _turn_call_from_message(assistant_msg: AIMessage) -> dict[str, Any] | None:
    for call in getattr(assistant_msg, "tool_calls", None) or []:
        if isinstance(call, dict) and str(call.get("name") or "") == "turn":
            return call
    return None


def _message_with_tool_calls(assistant_msg: AIMessage, tool_calls: list[dict[str, Any]]) -> AIMessage:
    return assistant_msg.model_copy(
        update={
            "tool_calls": tool_calls,
            "invalid_tool_calls": [],
            "additional_kwargs": {
                key: value
                for key, value in assistant_msg.additional_kwargs.items()
                if key != "tool_calls"
            },
        }
    )


def _handle_turn_stop_with_tools(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
) -> TurnControlResult:
    regular_calls = [
        call
        for call in (getattr(assistant_msg, "tool_calls", None) or [])
        if isinstance(call, dict) and str(call.get("name") or "") != "turn"
    ]
    if not regular_calls:
        return _handle_invalid_turn(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            estimate_tokens=lambda messages: loop.context_tokens,
        )

    if loop.pending_provisional is not None:
        terminal = normalize_terminal_message(loop.pending_provisional)
        terminal_visible = loop.pending_provisional_visible
    else:
        terminal = normalize_terminal_message(assistant_msg)
        terminal_visible = not loop.turn_prompt_active

    if not extract_text(terminal).strip():
        return _handle_invalid_turn(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            estimate_tokens=lambda messages: loop.context_tokens,
        )

    graph._turn_metrics.increment("turn_control_called")
    graph._pending_turn_stop_commit = {
        "terminal_msg": terminal,
        "terminal_msg_visible": terminal_visible,
    }
    loop.terminal_msg = _message_with_tool_calls(assistant_msg, regular_calls)
    loop.terminal_msg_visible = True
    return TurnControlResult(
        "break",
        llm_messages,
        loop.context_tokens,
        turn_state,
        runtime_task_state,
    )


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
    if loop.invalid_turn_repairs < 2:
        loop.invalid_turn_repairs += 1
        graph._turn_metrics.increment("turn_control_invalid")
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
    if loop.invalid_turn_repairs < 2:
        loop.invalid_turn_repairs += 1
        graph._turn_metrics.increment("turn_control_mixed_tools")
        graph._turn_metrics.increment("turn_control_invalid")
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
    protocol: Any,
    estimate_tokens: Any,
) -> TurnControlResult:
    text = extract_text(assistant_msg).strip()
    has_text = bool(text)
    is_turn_protocol = getattr(protocol, "protocol_id", "turn") == "turn"
    if (
        turn_state == "initial"
        and loop.missing_turn_count == 0
        and not loop.start_prompt_injected
        and not loop.turn_prompt_active
        and bool(state_messages and isinstance(state_messages[-1], HumanMessage))
        and has_text
    ):
        loop.terminal_msg = normalize_terminal_message(assistant_msg)
        loop.terminal_msg_visible = not loop.turn_prompt_active
        turn_state = "committed"
        return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
    if loop.missing_turn_count == 0 and has_text:
        loop.pending_provisional = assistant_msg
        loop.pending_provisional_visible = not loop.turn_prompt_active
    if not is_turn_protocol and has_text:
        loop.terminal_msg = normalize_terminal_message(assistant_msg)
        loop.terminal_msg_visible = loop.pending_provisional_visible
        turn_state = "committed"
        return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
    if (
        turn_state == "initial"
        and not loop.start_prompt_injected
        and loop.missing_turn_count == 0
        and len(text.splitlines()) > 3
        and interaction_mode_value not in {InteractionMode.PLAN.value, InteractionMode.GOAL.value}
    ):
        graph._turn_metrics.increment("turn_control_auto_committed")
        loop.terminal_msg = normalize_terminal_message(assistant_msg)
        loop.terminal_msg_visible = loop.pending_provisional_visible
        turn_state = "committed"
        return TurnControlResult("break", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
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
    if loop.missing_turn_count == 1 and len(text.splitlines()) > 3:
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
    if loop.pending_provisional is None:
        return _invalid_turn_failure(llm_messages, loop, turn_state, runtime_task_state)
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
