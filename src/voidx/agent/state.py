"""Agent state — typed, explicit, every field has a known type."""

from __future__ import annotations

from typing import Annotated, Any

from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from typing_extensions import NotRequired, TypedDict

from voidx.agent.task_state import TaskState, TodoRunState


class AgentState(TypedDict):
    """Complete agent state. LangGraph manages this across the graph."""
    messages: Annotated[list[BaseMessage], add_messages]  # LangGraph auto-merge
    workspace: str  # absolute path to working directory
    persona: str  # runtime thinking mode (coordinate/explore/plan/implement/review)
    plan_mode: bool  # when True, write/edit are denied — plan→implement→review enforced
    interaction_mode: str  # auto/plan/goal
    task_state: NotRequired[TaskState | dict[str, Any]]
    todo_state: NotRequired[TodoRunState | dict[str, Any] | None]
    user_message_id: NotRequired[int]
    tool_results: dict[str, str]  # tool_call_id → result text
    step_count: int  # current step number
    should_continue: bool  # router flag
    convergence_forced: NotRequired[bool]  # final no-tools convergence prompt was injected
    turn_state: NotRequired[str]  # initial/running/committed within one user turn
