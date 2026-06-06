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
from datetime import datetime
from typing import TYPE_CHECKING

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.graph import END, StateGraph

from voidx.agent.agents import BASE_SYSTEM_PROMPT, PLAN_MODE_APPEND, get_agent, AgentDef
from voidx.agent.graph.compaction import GraphCompactionMixin
from voidx.agent.graph.convergence import (
    build_convergence_messages,
    generate_fallback_summary,
    is_step_hint_message,
)
from voidx.agent.graph.permissions import GraphPermissionMixin
from voidx.agent.graph.runtime import (
    console,
    current_parent_tool_call_id as _current_parent_tool_call_id,
    ui,
)
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.state import AgentState
from voidx.agent.graph.streaming import extract_text, stream_llm as _stream_llm
from voidx.agent.graph.subagent import run_subagent as _run_subagent
from voidx.agent.graph.tool_execution import GraphToolExecutionMixin
from voidx.agent.intent_refinement import refine_intent
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
from voidx.memory.session import MessageRow, SessionInfo
from voidx.permission.service import PermissionService
from voidx.tools.registry import ToolRegistry
from voidx.tools.agent import AgentTool
from voidx.tools.on_intent import OnIntentInput, OnIntentTool
from voidx.tools.task_tracker import TaskTracker
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.dock import dock
from voidx.ui.output.events import (
    SubagentFinished,
    SubagentStarted,
    ui_events,
    via_events,
)
from voidx.ui.output.tree import OutputNode, OutputTree

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphComponentHost
    from voidx.ui.tui import PureTui


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

        # Build tool registry, wire tracker through registry
        self._tracker = TaskTracker()
        self.tools = ToolRegistry(settings=settings, tracker=self._tracker)
        intent_tool = OnIntentTool(resolver=self._resolve_on_intent)
        self.tools.register("on_intent", intent_tool, intent_tool.description, intent_tool.parameters_schema())
        agent_tool = AgentTool(runner=self._subagent_runner)
        self.tools.register("agent", agent_tool, agent_tool.description, agent_tool.parameters_schema())

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
        self._current_messages: list[BaseMessage] | None = None
        self._sub_buffers: dict[str, list[BaseMessage]] = {}
        # One-turn summary injection vs. persisted summary restored across turns.
        self._pending_summary: str | None = None
        self._compaction_summary: str = ""
        self._session_date: str = _session_date(session)
        self._session_msg_cache: list[MessageRow] | None = None
        self._app: PureTui | None = None
        self._next_agent_id: int = 0
        self._task_state = TaskState()
        self._task_run = TaskRun()
        self._needs_failure_check: dict[str, dict] = {}

        # Context compaction service — provider-aware limits
        from voidx.llm.provider import get_context_limit
        context_limit = get_context_limit(config.model.provider)
        self._usage_stats = UsageStats(context_limit=context_limit)
        self._compaction = CompactionService(
            context_limit=context_limit,
            output_token_max=config.model.max_tokens,
        )

        self._build()
        from voidx.agent.slash import SlashHandler

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
        if TYPE_CHECKING:
            _host_contract: GraphComponentHost = self

    @property
    def app(self) -> PureTui | None:
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

    def _resolve_on_intent(self, inp: OnIntentInput, ctx):
        return refine_intent(
            inp,
            ctx,
            config=self.config,
            settings=self._settings,
            registered_tool_ids=self.tools.ids(),
        )

    async def _subagent_runner(self, agent_def: AgentDef, description: str, model_override: str | None) -> str:
        parent_messages = getattr(self, '_current_messages', None)
        sub_buffer: list[BaseMessage] = []
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
            kwargs = {
                "parent_messages": parent_messages,
                "sub_messages": sub_buffer,
                "authorize_tools": authorize,
                "debug": self._debug,
                "agent_id": agent_id,
                "session_id": session_id if self._session else None,
                "usage_stats": self._usage_stats,
                "lsp_manager": getattr(self, "_lsp_manager", None),
                "skill_selection": self._settings.get_skill_selection() if self._settings else None,
            }
            if self._current_tree and self._turn_node:
                kwargs.update({
                    "capture_tree": self._current_tree,
                    "parent_node": self._turn_node,
                })
            result = await _run_subagent(
                agent_def,
                description,
                model_override,
                self.api_key,
                self.config,
                self._tracker,
                **kwargs,
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
            scope=_pending_approval_scope(state.get("pending_approval")) or state.get("goal") or latest_user_text,
            turn_count=state.get("goal_turn_count", 0),
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
            skill_runs=skill_context.runs,
            active_skill_summaries=skill_context.active,
            summary=summary,
            current_user_text=latest_user_text,
            task_intent=state.get("task_intent"),
            intent_resolution_reason=state.get("intent_resolution_reason", ""),
            pending_approval=state.get("pending_approval"),
            goal=state.get("goal", ""),
            goal_phase=state.get("goal_phase", ""),
            goal_status=state.get("goal_status", ""),
            goal_turn_count=state.get("goal_turn_count", 0),
            available_tool_ids=state.get("available_tool_ids", []),
            intent_confidence=state.get("intent_confidence"),
            intent_source=state.get("intent_source", ""),
            intent_refined=state.get("intent_refined", False),
            session_date=self._session_date,
        ).build()
        context.apply_to_messages(state.get("messages", []))

        return {**base, "skill_runs": skill_context.runs}

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
        if "available_tool_ids" in state:
            visible = set(state.get("available_tool_ids") or [])
            tool_defs = [t for t in tool_defs if t["function"]["name"] in visible]
        tool_defs = filter_unavailable_lsp_tools(tool_defs, getattr(self, "_lsp_manager", None))

        has_tool_budget = step < max_s - 1
        if not has_tool_budget:
            tool_defs = []
        convergence_messages, convergence_forced = build_convergence_messages(
            step=step,
            max_steps=max_s,
            has_tool_budget=has_tool_budget,
            goal=state.get("goal", "") or _latest_user_text(state.get("messages", [])),
        )
        llm_messages = [*state["messages"], *convergence_messages]

        agent_name = state.get("agent", "orchestrator")
        if self._debug:
            ui.print()
        ui.step_header(step, max_s, agent_name)

        # ── LLM call with retry ────────────────────────────────────────
        context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
        self._usage_stats.update_context(context_tokens)
        if self._session is not None:
            await save_context_frame_from_messages(
                session_id=self._session.id,
                user_message_id=state.get("user_message_id"),
                frame_kind="main",
                agent_role=agent_name,
                provider=self.config.model.provider,
                model=self.config.model.model,
                messages=llm_messages,
                token_estimate=context_tokens,
                metadata={
                    "step": step,
                    "max_steps": max_s,
                    "tool_count": len(tool_defs),
                    "convergence_hint_count": len(convergence_messages),
                    "convergence_forced": convergence_forced,
                },
            )
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                renderer = StreamingRenderer(console, debug=self._debug)
                model_with_tools = self.model.bind_tools(tool_defs) if tool_defs else self.model
                assistant_msg = await _stream_llm(model_with_tools, llm_messages, renderer, resolve_protocol(self.config.model))
                self._usage_stats.record_call(
                    extract_token_usage(assistant_msg),
                    fallback_input_tokens=context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, self.config.model.model),
                    messages=llm_messages,
                    model=self.config.model.model,
                    cache_key=f"{self.config.model.provider}/{self.config.model.model}",
                )
                if self._debug or not assistant_msg.tool_calls:
                    ui.print()
                break
            except Exception as e:
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

        return {
            "messages": [assistant_msg],
            "step_count": step + 1,
            "convergence_forced": convergence_forced,
        }

    def _router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            if state.get("step_count", 0) >= state.get("max_steps", 50):
                return "end"
            return "execute"
        return "end"

    async def _finalize(self, state: AgentState) -> dict:
        if not state.get("convergence_forced"):
            return {}
        last = _latest_ai_message(state.get("messages", []))
        if isinstance(last, AIMessage) and not last.tool_calls:
            if len(extract_text(last).strip()) >= 20:
                return {}
        return {"messages": [AIMessage(content=generate_fallback_summary(state))]}


def _latest_user_text(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) and not is_step_hint_message(msg):
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


def _latest_ai_message(messages: list[BaseMessage]) -> AIMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _session_date(session: SessionInfo | None) -> str:
    if session is not None and session.created_at:
        try:
            return datetime.fromisoformat(session.created_at).astimezone().strftime("%Y-%m-%d %Z")
        except ValueError:
            pass
    return datetime.now().astimezone().strftime("%Y-%m-%d %Z")


def _pending_approval_scope(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("scope") or "").strip()
    return str(getattr(value, "scope", "") or "").strip()
