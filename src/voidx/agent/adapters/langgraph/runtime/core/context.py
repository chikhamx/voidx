from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from voidx.agent.application.message_trimming import trim_superseded_file_tools
from voidx.agent.application.runtime_context import ContextCompiler
from voidx.agent.domain.task.state import TaskState
from voidx.agent.adapters.persistence.context_frame_repository import save_context_frame_from_messages


def rebuild_llm_messages(
    messages: list[BaseMessage],
    guidance_messages: list[HumanMessage],
    *,
    allow_inline_compaction: bool,
    compaction_happened: bool,
    inline_compaction_guide_for: Callable[[list[BaseMessage]], HumanMessage | None],
) -> tuple[list[BaseMessage], list[HumanMessage], bool]:
    base_messages = trim_superseded_file_tools([*messages, *guidance_messages])
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
) -> list[BaseMessage]:
    if builder is None:
        return messages
    builder.turn_state = new_turn_state
    if task_state is not None:
        builder.task_state = task_state
        builder.current_goal = task_state.current_goal
        builder.task_intent = task_state.current_intent
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
