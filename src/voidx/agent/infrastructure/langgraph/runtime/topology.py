"""LangGraph topology and pure graph helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import END, StateGraph

from voidx.agent.infrastructure.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.state import AgentState
from voidx.llm.message_markers import is_guidance_message

from voidx.memory.service import SessionInfo


def build_graph(host: Any):
    workflow = StateGraph(AgentState)

    workflow.add_node("prepare", host._prepare_with_stream)
    workflow.add_node("call_llm", host._call_llm)
    workflow.add_node("execute_tools", host._execute_tools)
    workflow.add_node("finalize", host._finalize)

    workflow.set_entry_point("prepare")
    workflow.add_edge("prepare", "call_llm")
    workflow.add_conditional_edges("call_llm", host._router, {
        "execute": "execute_tools",
        "end": "finalize",
    })
    workflow.add_conditional_edges("execute_tools", route_after_execute_tools, {
        "call_llm": "call_llm",
        "end": "finalize",
    })
    workflow.add_edge("finalize", END)

    return workflow.compile()


def prepare_state(state: AgentState) -> dict:
    return {
        "step_count": state.get("step_count", 0) + 1,
    }


def route_after_execute_tools(state: AgentState) -> str:
    if state.get("should_continue") is False:
        return "end"
    return "call_llm"


def latest_user_text(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages):
        if (
            isinstance(msg, HumanMessage)
            and not is_step_hint_message(msg)
            and not is_guidance_message(msg)
        ):
            content = msg.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if isinstance(text, str):
                            parts.append(text)
                return "\n".join(parts)
            return str(content)
    return ""


def latest_ai_message(messages: list[BaseMessage]) -> AIMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def session_date(session: SessionInfo | None) -> str:
    if session is not None and session.created_at:
        try:
            return datetime.fromisoformat(session.created_at).astimezone().strftime("%Y-%m-%d %Z")
        except ValueError:
            pass
    return datetime.now().astimezone().strftime("%Y-%m-%d %Z")
