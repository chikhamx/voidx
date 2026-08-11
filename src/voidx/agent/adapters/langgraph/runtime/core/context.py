from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from voidx.agent.application.message_trimming import trim_superseded_file_tools
from voidx.agent.application.runtime_context import ContextCompiler
from voidx.agent.domain.task.state import TaskState
from voidx.agent.adapters.persistence.context_frame_repository import save_context_frame_from_messages


def _strip_consumed_images(messages: list[BaseMessage]) -> list[BaseMessage]:
    result = list(messages)
    consumed = False
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if isinstance(message, (AIMessage, ToolMessage)):
            consumed = True
            continue
        if not consumed or not isinstance(message, HumanMessage):
            continue
        content = message.content
        if not isinstance(content, list):
            continue
        filtered = [
            part
            for part in content
            if not isinstance(part, dict)
            or part.get("type") not in {"image", "image_url"}
        ]
        if len(filtered) == len(content):
            continue
        result[index] = message.model_copy(update={"content": filtered or ""})
    return result


def rebuild_llm_messages(
    messages: list[BaseMessage],
    guidance_messages: list[HumanMessage],
    *,
    allow_inline_compaction: bool,
    compaction_happened: bool,
    inline_compaction_guide_for: Callable[[list[BaseMessage]], HumanMessage | None],
    message_token_estimator: Callable[[BaseMessage], int] | None = None,
) -> tuple[list[BaseMessage], list[HumanMessage], bool]:
    base_messages = trim_superseded_file_tools(
        _strip_consumed_images([*messages, *guidance_messages]),
        token_estimator=message_token_estimator,
    )
    if allow_inline_compaction and not compaction_happened:
        inline_compaction_guide = inline_compaction_guide_for(base_messages)
        if inline_compaction_guide is not None:
            base_messages.append(inline_compaction_guide)
    return base_messages, [], False


async def save_main_context_frame(
    *,
    session: Any,
    user_message_id: Any,
    persona: str,
    provider: str,
    model: str,
    messages: list[BaseMessage],
    token_estimate: int,
    step: int,
    tool_count: int,
    convergence_messages: list[HumanMessage],
    convergence_forced: bool,
) -> None:
    if session is None:
        return
    await save_context_frame_from_messages(
        session_id=session.id,
        user_message_id=user_message_id,
        frame_kind="main",
        agent_persona=persona,
        provider=provider,
        model=model,
        messages=messages,
        token_estimate=token_estimate,
        metadata={
            "step": step,
            "tool_count": tool_count,
            "convergence_hint_count": len(convergence_messages),
            "convergence_forced": convergence_forced,
        },
    )


def rerender_task_context(
    builder: Any,
    messages: list[BaseMessage],
    new_turn_state: str,
    task_state: TaskState | None = None,
    *,
    persona: str | None = None,
) -> list[BaseMessage]:
    if builder is None:
        return messages
    builder.turn_state = new_turn_state
    if persona is not None:
        builder.persona = persona
    if task_state is not None:
        builder.task_state = task_state
        builder.current_goal = task_state.current_goal
        builder.task_intent = task_state.current_intent
        builder.workflow_route = task_state.workflow_route
        builder.workflow_runs = list(task_state.workflow_runs.values())
        builder.todo_state = task_state.todo_state
        builder.active_workflow_summaries = [
            (
                f"{run.name} ({run.reason})"
                if run.reason.strip()
                else run.name
            )
            for run in task_state.workflow_runs.values()
            if getattr(run.status, "value", run.status) == "active"
        ]
    context = builder.build()
    return ContextCompiler(context).compile_messages(messages)


def replacement_messages(
    assistant_msg: AIMessage,
    *,
    compaction_happened: bool,
    state_messages: list[BaseMessage],
    pending_message_delta: list[BaseMessage] | None = None,
) -> list[BaseMessage]:
    if not compaction_happened:
        return [*(pending_message_delta or []), assistant_msg]
    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *state_messages,
        assistant_msg,
    ]
