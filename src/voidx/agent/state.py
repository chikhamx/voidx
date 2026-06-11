"""Agent state — typed, explicit, every field has a known type."""

from __future__ import annotations

from typing import Annotated, Any

from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from typing_extensions import NotRequired, TypedDict

from voidx.agent.task_state import PendingApproval, TodoRunState
from voidx.workflow.runtime import WorkflowRunState


class AgentState(TypedDict):
    """Complete agent state. LangGraph manages this across the graph."""
    messages: Annotated[list[BaseMessage], add_messages]  # LangGraph auto-merge
    workspace: str  # absolute path to working directory
    agent: str  # current agent name (orchestrator/explore/plan/implement/review)
    plan_mode: bool  # when True, write/edit are denied — plan→implement→review enforced
    interaction_mode: str  # auto/plan/goal
    task_intent: str  # chat/inspect/design/review/implement/debug/ambiguous
    intent_resolution_reason: str
    pending_approval: NotRequired[PendingApproval | dict[str, Any] | None]
    goal: str
    goal_phase: str
    goal_status: str
    goal_turn_count: int
    workflow_runs: NotRequired[list[WorkflowRunState]]
    available_tool_ids: NotRequired[list[str]]
    intent_confidence: NotRequired[float]
    intent_source: NotRequired[str]
    intent_refined: NotRequired[bool]
    todo_state: NotRequired[TodoRunState | dict[str, Any] | None]
    user_message_id: NotRequired[int]
    tool_results: dict[str, str]  # tool_call_id → result text
    step_count: int  # current step number
    max_steps: int  # safety limit
    should_continue: bool  # router flag
    convergence_forced: NotRequired[bool]  # final no-tools convergence prompt was injected
