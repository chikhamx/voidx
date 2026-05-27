"""Agent state — typed, explicit, every field has a known type."""

from __future__ import annotations

from typing import Annotated, Any

from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from typing_extensions import TypedDict


class AgentState(TypedDict):
    """Complete agent state. LangGraph manages this across the graph."""
    messages: Annotated[list[BaseMessage], add_messages]  # LangGraph auto-merge
    workspace: str  # absolute path to working directory
    agent: str  # current agent name (orchestrator/explore/plan/implement/review)
    plan_mode: bool  # when True, write/edit are denied — plan→implement→review enforced
    tool_results: dict[str, str]  # tool_call_id → result text
    step_count: int  # current step number
    max_steps: int  # safety limit
    should_continue: bool  # router flag
