"""Agent graph — LangGraph state machine with 5-agent system.

Agents:
  orchestrator — primary, coordinates, can make small direct edits
  explore     — read-only codebase search
  plan        — read-only architecture design
  implement   — delegated coding agent for broad or isolated changes
  review      — read-only code review (PASS/FAIL/NEEDS_CHANGE)

Depth limit = 1: child agents cannot start further child agents.
"""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.graph import END, StateGraph

from voidx.agent.agents import BASE_SYSTEM_PROMPT, PLAN_MODE_APPEND, get_agent, AgentDef
from voidx.agent.graph_components.compaction import GraphCompactionMixin
from voidx.agent.graph_components.permissions import GraphPermissionMixin
from voidx.agent.graph_components.runtime import (
    console,
    current_parent_tool_call_id as _current_parent_tool_call_id,
    ui,
)
from voidx.agent.graph_components.run_loop import GraphRunLoopMixin
from voidx.agent.state import AgentState
from voidx.agent.graph_components.streaming import stream_llm as _stream_llm
from voidx.agent.graph_components.subagent import run_subagent as _run_subagent
from voidx.agent.graph_components.tool_execution import GraphToolExecutionMixin
from voidx.agent.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.agent.task_state import TaskRun, TaskState
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
from voidx.config import Config, Settings
from voidx.llm.compaction import CompactionService
from voidx.llm.instruction import InstructionService
from voidx.llm.provider import create_chat_model, resolve_protocol
from voidx.llm.usage import (
    UsageStats,
    estimate_context_tokens,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.memory.session import SessionInfo
from voidx.agent.slash import SlashHandler
from voidx.permission.service import PermissionService
from voidx.tools.registry import ToolRegistry
from voidx.tools.agent import AgentTool
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.todo import TodoWriteTool
from voidx.ui.console import StreamingRenderer
from voidx.ui.dock import dock
from voidx.ui.events import (
    SubagentFinished,
    SubagentStarted,
    ui_events,
    via_events,
)
from voidx.ui.tree import OutputNode


# ── LangGraph nodes ────────────────────────────────────────────────────────

def _prepare(state: AgentState) -> dict:
    """Advance step counters before LLM execution."""
    agent_name = state.get("agent", "orchestrator")
    agent_def = get_agent(agent_name)

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

        # Bind settings to catalog so list_models() merges custom models
        if settings:
            from voidx.llm.catalog import bind_settings
            bind_settings(settings)

        # Build tool registry, wire agent/todo/task_status to tracker
        self.tools = ToolRegistry(settings=settings)
        self._tracker = TaskTracker()
        agent_tool = AgentTool(runner=self._subagent_runner)
        self.tools.register("agent", agent_tool, agent_tool.description, agent_tool.parameters_schema())
        task_status_tool = TaskStatusTool(tracker=self._tracker)
        self.tools.register("task_status", task_status_tool, task_status_tool.description, task_status_tool.parameters_schema())
        # Replace built-in todo with tracker-aware version
        todo_tool = TodoWriteTool(tracker=self._tracker)
        self.tools.register("todo", todo_tool, todo_tool.description, todo_tool.parameters_schema())

        # AGENTS.md instruction service — refreshed each turn
        self._instruction = InstructionService(self._workspace, settings=settings)

        # Permission service — sandbox → allow/deny/ask per tool call
        self._permission = PermissionService(
            permission_mode=config.permission_mode.value,
            sandbox_mode=config.sandbox_mode.value,
            sandbox_workspace_write=config.sandbox_workspace_write,
            approval_policy=config.approval_policy.value,
            approval_reviewer=config.approval_reviewer.value,
        )

        self._interaction_mode: InteractionMode = InteractionMode.AUTO
        self._debug: bool = True
        ui.set_debug(self._debug)

        # File mtime staleness guard — shared across tool calls
        self._file_mtimes: dict[str, float] = {}
        self._turn_node: OutputNode | None = None
        self._current_tree: OutputTree | None = None
        self._current_messages: list | None = None
        self._sub_buffers: dict[str, list] = {}
        self._pending_summary: str | None = None
        self._compaction_summary: str = ""
        self._app: Any | None = None  # PureTui (tui.py)
        self._next_agent_id: int = 0
        self._task_state = TaskState()
        self._task_run = TaskRun()

        # Context compaction service — provider-aware limits
        from voidx.llm.provider import get_context_limit
        context_limit = get_context_limit(config.model.provider)
        self._usage_stats = UsageStats(context_limit=context_limit)
        self._compaction = CompactionService(
            context_limit=context_limit,
            output_token_max=config.model.max_tokens,
        )

        self._build()
        self._slash = SlashHandler(self)

        # MCP (Model Context Protocol) servers — start on run()
        from voidx.mcp import McpManager
        self._mcp_manager = McpManager(
            settings=self._settings,
            registry=self.tools,
            permission=self._permission,
        )
        from voidx.lsp import LspManager
        self._lsp_manager = LspManager(self._workspace)

    @property
    def app(self) -> Any | None:
        """The interactive TUI app, if one is running."""
        return self._app

    @property
    def _plan_mode(self) -> bool:
        return self._interaction_mode == InteractionMode.PLAN

    @_plan_mode.setter
    def _plan_mode(self, value: bool) -> None:
        self._interaction_mode = InteractionMode.PLAN if value else InteractionMode.AUTO

    def set_interaction_mode(self, mode: str | InteractionMode) -> InteractionMode:
        self._interaction_mode = InteractionMode.parse(mode)
        return self._interaction_mode

    def interaction_mode(self) -> InteractionMode:
        return self._interaction_mode

    async def _subagent_runner(self, agent_def: AgentDef, description: str, model_override: str | None) -> str:
        parent_messages = getattr(self, '_current_messages', None)
        sub_buffer: list = []
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
                interaction_mode=self._interaction_mode.value,
            )

        if via_events():
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
                result = await _run_subagent(
                    agent_def,
                    description,
                    model_override,
                    self.api_key,
                    self.config,
                    self._tracker,
                    self._current_tree,
                    parent,
                    parent_messages=parent_messages,
                    sub_messages=sub_buffer,
                    authorize_tools=authorize,
                    debug=self._debug,
                    agent_id=agent_id,
                    session_id=session_id if self._session else None,
                    usage_stats=self._usage_stats,
                    lsp_manager=getattr(self, "_lsp_manager", None),
                    skill_selection=self._settings.get_skill_selection() if self._settings else None,
                )
            else:
                result = await _run_subagent(
                    agent_def,
                    description,
                    model_override,
                    self.api_key,
                    self.config,
                    self._tracker,
                    parent_messages=parent_messages,
                    sub_messages=sub_buffer,
                    authorize_tools=authorize,
                    debug=self._debug,
                    agent_id=agent_id,
                    session_id=session_id if self._session else None,
                    usage_stats=self._usage_stats,
                    lsp_manager=getattr(self, "_lsp_manager", None),
                    skill_selection=self._settings.get_skill_selection() if self._settings else None,
                )
            ok = True
            key = parent_tool_call_id or f"agent:{agent_id}"
            self._sub_buffers.setdefault(key, []).extend(sub_buffer)
            return result
        finally:
            if via_events():
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
        agent_name = state.get("agent", "orchestrator")
        self._current_agent = get_agent(agent_name)
        role_prompt = self._current_agent.role_prompt if self._current_agent else ""
        tool_contract = self._current_agent.tool_contract if self._current_agent else ""

        interaction_mode = state.get("interaction_mode") or (
            InteractionMode.PLAN.value if state.get("plan_mode", False) else self._interaction_mode.value
        )
        latest_user_text = _latest_user_text(state.get("messages", []))
        instructions = await self._instruction.system()
        skill_context = await self._instruction.skill_context_for(
            latest_user_text,
            agent=agent_name,
            task_intent=state.get("task_intent"),
            interaction_mode=interaction_mode,
        )
        mode_prompt = PLAN_MODE_APPEND if InteractionMode.parse(interaction_mode) == InteractionMode.PLAN else ""
        summary = self._pending_summary or self._compaction_summary
        self._pending_summary = None

        context = RuntimeContextBuilder(
            config=self.config,
            workspace=state.get("workspace", "."),
            base_system_prompt=BASE_SYSTEM_PROMPT,
            role_prompt=role_prompt,
            mode_prompt=mode_prompt,
            tool_contract=tool_contract,
            agent=agent_name,
            interaction_mode=interaction_mode,
            instructions=instructions,
            skill_instructions=skill_context.instructions,
            active_skill_summaries=skill_context.active,
            summary=summary,
            current_user_text=latest_user_text,
            task_intent=state.get("task_intent"),
            implementation_allowed=state.get("implementation_allowed"),
            intent_resolution_reason=state.get("intent_resolution_reason", ""),
            awaiting_implementation_approval=state.get("awaiting_implementation_approval", False),
            approved_scope=state.get("approved_scope", ""),
            goal=state.get("goal", ""),
            goal_phase=state.get("goal_phase", ""),
            goal_status=state.get("goal_status", ""),
            goal_turn_count=state.get("goal_turn_count", 0),
        ).build()
        context.apply_to_messages(state.get("messages", []))

        return base

    async def _call_llm(self, state: AgentState) -> dict:
        step = state.get("step_count", 0)
        max_s = state.get("max_steps", 50)
        if step > max_s:
            return {"should_continue": False}

        if self.model is None:
            return {
                "messages": [AIMessage(content=(
                    "No model configured. Use /model new to create a profile."
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
        tool_defs = filter_unavailable_lsp_tools(tool_defs, getattr(self, "_lsp_manager", None))

        has_tool_budget = step < max_s - 1
        if not has_tool_budget:
            tool_defs = []

        agent_name = state.get("agent", "orchestrator")
        if self._debug:
            ui.print()
        ui.step_header(step, max_s, agent_name)

        # ── LLM call with retry ────────────────────────────────────────
        context_tokens = estimate_context_tokens(state["messages"], self.config.model.model)
        self._usage_stats.update_context(context_tokens)
        if self._session is not None:
            await save_context_frame_from_messages(
                session_id=self._session.id,
                user_message_id=state.get("user_message_id"),
                frame_kind="main",
                agent_role=agent_name,
                provider=self.config.model.provider,
                model=self.config.model.model,
                messages=state["messages"],
                token_estimate=context_tokens,
                metadata={
                    "step": step,
                    "max_steps": max_s,
                    "tool_count": len(tool_defs),
                },
            )
        max_retries = 2
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                renderer = StreamingRenderer(console, debug=self._debug)
                model_with_tools = self.model.bind_tools(tool_defs) if tool_defs else self.model
                assistant_msg = await _stream_llm(model_with_tools, state["messages"], renderer, resolve_protocol(self.config.model))
                self._usage_stats.record_call(
                    extract_token_usage(assistant_msg),
                    fallback_input_tokens=context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, self.config.model.model),
                    messages=state["messages"],
                    model=self.config.model.model,
                    cache_key=f"{self.config.model.provider}/{self.config.model.model}",
                )
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


def _latest_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
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
