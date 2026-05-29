"""Agent graph — LangGraph state machine with 5-agent system.

Agents:
  orchestrator — primary, delegates, never writes code
  explore     — read-only codebase search
  plan        — read-only architecture design
  implement   — writes code, runs shell
  review      — read-only code review (PASS/FAIL/NEEDS_CHANGE)

Depth limit = 1: sub-agents cannot spawn further sub-agents.
"""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import (
    AIMessage,
    SystemMessage,
)
from langgraph.graph import END, StateGraph

from voidx.agent.agents import get_agent, AgentDef
from voidx.agent.graph_parts.compaction import GraphCompactionMixin
from voidx.agent.graph_parts.permissions import GraphPermissionMixin
from voidx.agent.prompts import SYSTEM_PROMPT
from voidx.agent.graph_parts.runtime import (
    console,
    current_parent_tool_call_id as _current_parent_tool_call_id,
    ui,
)
from voidx.agent.graph_parts.run_loop import GraphRunLoopMixin
from voidx.agent.state import AgentState
from voidx.agent.graph_parts.streaming import stream_llm as _stream_llm
from voidx.agent.graph_parts.subagent import run_subagent as _run_subagent
from voidx.agent.graph_parts.tool_execution import GraphToolExecutionMixin
from voidx.config import Config, Settings
from voidx.llm.compaction import CompactionService
from voidx.llm.instruction import InstructionService
from voidx.llm.provider import create_chat_model, resolve_protocol
from voidx.memory.session import SessionInfo
from voidx.agent.slash import SlashHandler
from voidx.permission.service import PermissionService
from voidx.tools.registry import ToolRegistry
from voidx.tools.task import TaskTool
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.todo import TodoWriteTool
from voidx.ui.console import StreamingRenderer
from voidx.ui.dock import dock
from voidx.ui.events import (
    SubagentFinished,
    SubagentStarted,
    ui_events,
)
from voidx.ui.tree import OutputNode


# ── LangGraph nodes ────────────────────────────────────────────────────────

def _prepare(state: AgentState) -> dict:
    """Inject system prompt + agent context."""
    agent_name = state.get("agent", "orchestrator")
    agent_def = get_agent(agent_name)
    agent_prompt = agent_def.prompt if agent_def else SYSTEM_PROMPT

    workspace = state.get("workspace", ".")
    system = f"{agent_prompt}\n\nCurrent workspace: {workspace}"

    msgs = state.get("messages", [])
    if not any(isinstance(m, SystemMessage) for m in msgs):
        msgs.insert(0, SystemMessage(content=system))

    return {
        "step_count": state.get("step_count", 0) + 1,
        "max_steps": state.get("max_steps", agent_def.max_steps if agent_def else 50),
    }


class VoidXGraph(
    GraphRunLoopMixin,
    GraphCompactionMixin,
    GraphToolExecutionMixin,
    GraphPermissionMixin,
):
    """The voidx agent as a LangGraph state machine."""

    def __init__(self, config: Config, api_key: str | None, session: SessionInfo | None = None, settings: Settings | None = None):
        self.config = config
        self.api_key = api_key
        self.model = create_chat_model(api_key, config.model) if api_key else None
        self._session = session
        self._workspace = config.workspace
        self._settings = settings

        # Build tool registry, wire task/todo/task_status to tracker
        self.tools = ToolRegistry(settings=settings)
        self._tracker = TaskTracker()
        task_tool = TaskTool(orchestrator_func=self._subagent_runner)
        self.tools.register("task", task_tool, task_tool.description, task_tool.parameters_schema())
        task_status_tool = TaskStatusTool(tracker=self._tracker)
        self.tools.register("task_status", task_status_tool, task_status_tool.description, task_status_tool.parameters_schema())
        # Replace built-in todo with tracker-aware version
        todo_tool = TodoWriteTool(tracker=self._tracker)
        self.tools.register("todo", todo_tool, todo_tool.description, todo_tool.parameters_schema())

        # AGENTS.md instruction service — refreshed each turn
        self._instruction = InstructionService(self._workspace)

        # Permission service — allow/deny/ask per tool call
        self._permission = PermissionService()

        # Plan mode — toggled by /plan and /unplan
        self._plan_mode: bool = False
        self._debug: bool = True
        ui.set_debug(self._debug)

        # File mtime staleness guard — shared across tool calls
        self._file_mtimes: dict[str, float] = {}
        self._turn_node: OutputNode | None = None
        self._current_tree: OutputTree | None = None
        self._current_messages: list | None = None
        self._pending_summary: str | None = None
        self._app: PromptToolkitTui | None = None
        self._next_agent_id: int = 0

        # Context compaction service — provider-aware limits
        from voidx.llm.provider import get_context_limit
        context_limit = get_context_limit(config.model.provider)
        self._compaction = CompactionService(
            context_limit=context_limit,
            output_token_max=config.model.max_tokens,
        )

        self._build()
        self._slash = SlashHandler(self)

    async def _subagent_runner(self, agent_def: AgentDef, description: str, model_override: str | None) -> str:
        parent_messages = getattr(self, '_current_messages', None)
        self._sub_buffer = []
        session_id = self._session.id if self._session else "default"
        agent_id = self._next_agent_id
        self._next_agent_id += 1
        parent_tool_call_id = _current_parent_tool_call_id.get()
        started_at = time.monotonic()

        async def authorize(calls, agent_name: str):
            return await self._authorize_tool_calls(
                calls,
                agent_name=agent_name,
                plan_mode=self._plan_mode,
                session_id=session_id,
            )

        if dock.active and ui_events.is_running:
            await ui_events.emit(SubagentStarted(
                agent_id=agent_id,
                subagent_id=f"agent_{agent_id}",
                name=agent_def.name,
                description=description,
                parent_agent_id=-1,
                parent_tool_call_id=parent_tool_call_id,
            ))

        ok = False
        try:
            if self._current_tree and self._turn_node:
                parent = self._turn_node
                result = await _run_subagent(agent_def, description, model_override, self.api_key, self.config, self._tracker, self._current_tree, parent, parent_messages=parent_messages, sub_messages=self._sub_buffer, authorize_tools=authorize, debug=self._debug, agent_id=agent_id)
            else:
                result = await _run_subagent(agent_def, description, model_override, self.api_key, self.config, self._tracker, parent_messages=parent_messages, sub_messages=self._sub_buffer, authorize_tools=authorize, debug=self._debug, agent_id=agent_id)
            ok = True
            return result
        finally:
            if dock.active and ui_events.is_running:
                await ui_events.emit(SubagentFinished(
                    agent_id=agent_id,
                    subagent_id=f"agent_{agent_id}",
                    ok=ok,
                    elapsed=time.monotonic() - started_at,
                ))

    def set_debug(self, value: bool) -> None:
        self._debug = value
        ui.set_debug(value)

    def _build(self) -> None:
        workflow = StateGraph(AgentState)

        workflow.add_node("prepare", self._prepare_with_stream)
        workflow.add_node("call_llm", self._call_llm)
        workflow.add_node("execute_tools", self._execute_tools)
        workflow.add_node("finalize", self._finalize)

        workflow.set_entry_point("prepare")
        workflow.add_edge("prepare", "call_llm")
        workflow.add_conditional_edges("call_llm", self._router, {
            "execute": "execute_tools",
            "end": "finalize",
        })
        workflow.add_edge("execute_tools", "call_llm")
        workflow.add_edge("finalize", END)

        self.graph = workflow.compile()

    # ── nodes ───────────────────────────────────────────────────────────

    async def _prepare_with_stream(self, state: AgentState) -> dict:
        base = _prepare(state)
        self._current_agent = get_agent(state.get("agent", "orchestrator"))

        # Inject AGENTS.md instructions into system prompt
        instructions = await self._instruction.system()
        if instructions:
            msgs = state.get("messages", [])
            if msgs and isinstance(msgs[0], SystemMessage):
                existing = msgs[0].content
                extra = "\n\n".join(instructions)
                msgs[0] = SystemMessage(content=f"{existing}\n\n{extra}")

        if state.get("plan_mode", False):
            from voidx.agent.agents import PLAN_MODE_APPEND
            msgs = state.get("messages", [])
            if msgs and isinstance(msgs[0], SystemMessage):
                msgs[0] = SystemMessage(content=f"{msgs[0].content}\n{PLAN_MODE_APPEND}")

        if self._pending_summary:
            msgs = state.get("messages", [])
            if msgs and isinstance(msgs[0], SystemMessage):
                msgs[0] = SystemMessage(
                    content=f"{msgs[0].content}\n\n## Conversation Summary\n{self._pending_summary}"
                )
                self._pending_summary = None

        return base

    async def _call_llm(self, state: AgentState) -> dict:
        step = state.get("step_count", 0)
        max_s = state.get("max_steps", 50)
        if step > max_s:
            return {"should_continue": False}

        if self.model is None:
            return {
                "messages": [AIMessage(content=(
                    "No model configured. Use /model config to create a profile."
                ))],
                "step_count": step,
                "should_continue": False,
            }

        agent = get_agent(state.get("agent", "orchestrator"))
        agent_tool_ids = agent.tools if agent else None
        all_tool_defs = self.tools.tools_for_llm()

        # Filter tools based on agent's allowlist
        if agent_tool_ids is not None:
            tool_defs = [t for t in all_tool_defs if t["function"]["name"] in agent_tool_ids]
        else:
            tool_defs = all_tool_defs

        agent_name = state.get("agent", "orchestrator")
        if self._debug:
            ui.print()
        ui.step_header(step, max_s, agent_name)

        # ── LLM call with retry ────────────────────────────────────────
        max_retries = 2
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                renderer = StreamingRenderer(console, debug=self._debug)
                model_with_tools = self.model.bind_tools(tool_defs) if tool_defs else self.model
                assistant_msg = await _stream_llm(model_with_tools, state["messages"], renderer, resolve_protocol(self.config.model))
                if self._debug or not assistant_msg.tool_calls:
                    ui.print()
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = (attempt + 1) * 2
                    ui.print(f"[dim]LLM error, retrying in {delay}s: {e}[/dim]")
                    await asyncio.sleep(delay)
                else:
                    ui.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
                    return {
                        "messages": [AIMessage(content=f"LLM call failed: {e}")],
                        "step_count": step,
                        "should_continue": False,
                    }
        else:
            # All retries exhausted
            return {
                "messages": [AIMessage(content=f"LLM call failed after all retries: {last_error}")],
                "step_count": step,
                "should_continue": False,
            }

        return {
            "messages": [assistant_msg],
            "step_count": step + 1,
        }

    def _router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            if state.get("step_count", 0) >= state.get("max_steps", 50):
                return "end"
            return "execute"
        return "end"

    async def _finalize(self, state: AgentState) -> dict:
        return {}
