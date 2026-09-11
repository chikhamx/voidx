from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from voidx.agent.adapters.langgraph.runtime.core.loop import LlmLoopState
from voidx.agent.adapters.langgraph.runtime.streaming import extract_text
from voidx.agent.adapters.langgraph.runtime.turn_control import (
    INVALID_TURN_PROMPT,
    TURN_INIT_PROMPT,
    TURN_TOOL_NAME,
    TurnClassification,
    _extract_goal_from_args,
    normalize_terminal_message,
)
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, TaskState
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
    stop_signal: str = ""


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
    from voidx.agent.adapters.langgraph.runtime.control_protocol import TurnToolProtocol

    protocol = protocol or TurnToolProtocol()
    classification = protocol.classify(assistant_msg)
    has_text = bool(extract_text(assistant_msg).strip())
    if _is_invalid_prompt_response(classification, has_text, loop) and not (
        getattr(protocol, "protocol_id", "") == "goal"
        and classification == TurnClassification.REGULAR_TOOLS
    ):
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
    repair_exhausted = getattr(protocol, "decision_repair_exhausted", None)
    if callable(repair_exhausted) and repair_exhausted(
        assistant_msg,
        loop,
        controller=loop_controller,
    ):
        reason = str(getattr(protocol, "failure_reason")())
        failure = AIMessage(content=f"Goal lifecycle protocol failed: {reason}.")
        return TurnControlResult(
            "fail",
            llm_messages,
            loop.context_tokens,
            turn_state,
            runtime_task_state,
            failure_msg=failure,
            stop_signal=reason,
        )

    if classification in {
        TurnClassification.VALID_INIT,
        TurnClassification.VALID_INIT_WITH_TOOLS,
    }:
        return await _handle_turn_init(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            estimate_tokens=estimate_tokens,
            rerender_task_context=rerender_task_context,
            with_tools=classification == TurnClassification.VALID_INIT_WITH_TOOLS,
            workflow_dag=workflow_dag,
        )

    if classification == TurnClassification.REGULAR_TOOLS:
        if loop.turn_prompt_active:
            graph._turn_metrics.increment("turn_control_prompt_succeeded")
        loop.turn_prompt_active = False
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
            state_messages=state_messages,
            rerender_task_context=rerender_task_context,
            workflow_dag=workflow_dag,
        )

    if classification == TurnClassification.PLAIN_TEXT:
        return _handle_plain_text(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            turn_state=turn_state,
            runtime_task_state=runtime_task_state,
            estimate_tokens=estimate_tokens,
            state_messages=state_messages,
            rerender_task_context=rerender_task_context,
            workflow_dag=workflow_dag,
        )

    graph._turn_metrics.increment("turn_control_prompt_succeeded")
    loop.terminal_msg = normalize_terminal_message(assistant_msg)
    loop.terminal_msg_visible = not loop.turn_prompt_active
    loop.turn_prompt_active = False
    return TurnControlResult("break", llm_messages, loop.context_tokens, "committed", runtime_task_state)


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
    return loop.turn_prompt_active and has_text and classification != TurnClassification.PLAIN_TEXT


def apply_turn_goal(
    *,
    graph: Any,
    runtime_task_state: TaskState,
    goal_text: str,
    workflow_dag: WorkflowDAG | None = None,
) -> None:
    resolution = GoalResolution(
        goal=GoalSpec(desc=goal_text),
        plan=None,
    )
    runtime_task_state.update_after_turn(resolution)
    if workflow_dag is not None:
        reconciled_workflow_runs = reconcile_workflow_runs_for_turn(
            goal_resolution=resolution,
            after_state=runtime_task_state,
            dag=workflow_dag,
        )
        runtime_task_state.workflow_runs = {
            run.name: run for run in reconciled_workflow_runs
        }
    if hasattr(graph, "_task_state"):
        graph._task_state = runtime_task_state.model_copy(deep=True)
    if hasattr(graph, "_invalidate_tui_for_turn"):
        graph._invalidate_tui_for_turn()
    elif hasattr(graph, "_ui") and hasattr(graph._ui, "invalidate"):
        graph._ui.invalidate()


async def _handle_turn_init(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
    estimate_tokens: Any,
    rerender_task_context: Any,
    with_tools: bool = False,
    workflow_dag: WorkflowDAG | None = None,
) -> TurnControlResult:
    init_call = _turn_call_from_message(assistant_msg)
    tool_call_id = str((init_call or {}).get("id") or "")
    regular_calls = [
        call
        for call in (getattr(assistant_msg, "tool_calls", None) or [])
        if isinstance(call, dict) and str(call.get("name") or "") != TURN_TOOL_NAME
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
                content="Turn already initialized.",
                tool_call_id=tool_call_id,
                name=TURN_TOOL_NAME,
            ),
        ]
        loop.context_tokens = estimate_tokens(llm_messages)
        return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)

    init_args = (init_call or {}).get("args") or {}
    goal_text = _extract_goal_from_args(init_args) or str(init_args.get("goal") or "").strip()
    apply_turn_goal(
        graph=graph,
        runtime_task_state=runtime_task_state,
        goal_text=goal_text,
        workflow_dag=workflow_dag,
    )
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
            content="Turn initialized. Continue the work.",
            tool_call_id=tool_call_id,
            name=TURN_TOOL_NAME,
        ),
    ]
    loop.context_tokens = estimate_tokens(llm_messages)
    return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)


def _turn_call_from_message(assistant_msg: AIMessage) -> dict[str, Any] | None:
    for call in getattr(assistant_msg, "tool_calls", None) or []:
        if isinstance(call, dict) and str(call.get("name") or "") == TURN_TOOL_NAME:
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


def _handle_invalid_turn(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
    estimate_tokens: Any,
    state_messages: list[BaseMessage] | None = None,
    rerender_task_context: Any | None = None,
    workflow_dag: WorkflowDAG | None = None,
) -> TurnControlResult:
    has_text = bool(extract_text(assistant_msg).strip())
    if has_text:
        graph._turn_metrics.increment("turn_control_invalid_committed")
        loop.terminal_msg = normalize_terminal_message(assistant_msg)
        loop.terminal_msg_visible = not loop.turn_prompt_active
        loop.turn_prompt_active = False
        return TurnControlResult("break", llm_messages, loop.context_tokens, "committed", runtime_task_state)
    if loop.invalid_turn_repairs < 2:
        loop.invalid_turn_repairs += 1
        graph._turn_metrics.increment("turn_control_invalid")
        loop.turn_prompt_active = True
        llm_messages = [
            *llm_messages,
            assistant_msg,
            HumanMessage(
                content=INVALID_TURN_PROMPT,
                additional_kwargs={GUIDANCE_MARKER: True},
            ),
        ]
        loop.context_tokens = estimate_tokens(llm_messages)
        return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
    has_legacy_turn = any(
        isinstance(call, dict) and str(call.get("name") or "") == "turn"
        for call in getattr(assistant_msg, "tool_calls", None) or []
    )
    if turn_state == "initial" and not has_legacy_turn:
        return _fallback_turn_init(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            runtime_task_state=runtime_task_state,
            estimate_tokens=estimate_tokens,
            state_messages=state_messages,
            rerender_task_context=rerender_task_context,
            workflow_dag=workflow_dag,
        )
    return _invalid_turn_failure(llm_messages, loop, turn_state, runtime_task_state)


def _handle_plain_text(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    turn_state: str,
    runtime_task_state: TaskState,
    estimate_tokens: Any,
    state_messages: list[BaseMessage] | None = None,
    rerender_task_context: Any | None = None,
    workflow_dag: WorkflowDAG | None = None,
) -> TurnControlResult:
    text = extract_text(assistant_msg).strip()
    if text:
        if loop.turn_prompt_active:
            graph._turn_metrics.increment("turn_control_prompt_succeeded")
        loop.terminal_msg = normalize_terminal_message(assistant_msg)
        loop.terminal_msg_visible = not loop.turn_prompt_active
        loop.turn_prompt_active = False
        return TurnControlResult("break", llm_messages, loop.context_tokens, "committed", runtime_task_state)

    if loop.invalid_turn_repairs < 2:
        loop.invalid_turn_repairs += 1
        graph._turn_metrics.increment("turn_control_missing")
        loop.turn_prompt_active = True
        prompt = TURN_INIT_PROMPT if turn_state == "initial" else INVALID_TURN_PROMPT
        llm_messages = [
            *llm_messages,
            assistant_msg,
            HumanMessage(
                content=prompt,
                additional_kwargs={GUIDANCE_MARKER: True},
            ),
        ]
        loop.context_tokens = estimate_tokens(llm_messages)
        return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)
    if turn_state == "initial":
        return _fallback_turn_init(
            graph=graph,
            assistant_msg=assistant_msg,
            llm_messages=llm_messages,
            loop=loop,
            runtime_task_state=runtime_task_state,
            estimate_tokens=estimate_tokens,
            state_messages=state_messages,
            rerender_task_context=rerender_task_context,
            workflow_dag=workflow_dag,
        )
    return _invalid_turn_failure(llm_messages, loop, turn_state, runtime_task_state)


def _fallback_turn_init(
    *,
    graph: Any,
    assistant_msg: AIMessage,
    llm_messages: list[BaseMessage],
    loop: LlmLoopState,
    runtime_task_state: TaskState,
    estimate_tokens: Any,
    state_messages: list[BaseMessage] | None = None,
    rerender_task_context: Any | None = None,
    workflow_dag: WorkflowDAG | None = None,
) -> TurnControlResult:
    from voidx.agent.adapters.langgraph.runtime.topology import latest_user_text

    graph._turn_metrics.increment("turn_control_third_miss_fallback")
    init_call = _turn_call_from_message(assistant_msg)
    tool_call_id = str((init_call or {}).get("id") or "")

    init_args = (init_call or {}).get("args") or {}
    goal_text = _extract_goal_from_args(init_args) or str(init_args.get("goal") or "").strip()
    if not goal_text and state_messages:
        goal_text = latest_user_text(state_messages)
    if not goal_text:
        goal_text = latest_user_text(llm_messages)
    if not goal_text:
        goal_text = "Continue the work"

    apply_turn_goal(
        graph=graph,
        runtime_task_state=runtime_task_state,
        goal_text=goal_text,
        workflow_dag=workflow_dag,
    )
    turn_state = "running"
    loop.turn_prompt_active = False
    if rerender_task_context is not None:
        llm_messages = rerender_task_context(llm_messages, "running", runtime_task_state)

    regular_calls = [
        call
        for call in (getattr(assistant_msg, "tool_calls", None) or [])
        if isinstance(call, dict) and str(call.get("name") or "") != TURN_TOOL_NAME
    ]
    if regular_calls:
        loop.terminal_msg = _message_with_tool_calls(assistant_msg, regular_calls)
        return TurnControlResult(
            "break",
            llm_messages,
            estimate_tokens(llm_messages),
            turn_state,
            runtime_task_state,
        )

    if tool_call_id:
        response_msg: BaseMessage = ToolMessage(
            content="Turn initialized automatically. Continue the work.",
            tool_call_id=tool_call_id,
            name=TURN_TOOL_NAME,
        )
    else:
        response_msg = HumanMessage(
            content="Turn initialized automatically. Continue the work.",
            additional_kwargs={GUIDANCE_MARKER: True},
        )

    llm_messages = [
        *llm_messages,
        assistant_msg,
        response_msg,
    ]
    loop.context_tokens = estimate_tokens(llm_messages)
    return TurnControlResult("retry", llm_messages, loop.context_tokens, turn_state, runtime_task_state)


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
