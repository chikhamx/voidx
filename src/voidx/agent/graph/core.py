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
from typing import TYPE_CHECKING

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
)

from voidx.agent.agents import (
    BASE_SYSTEM_PROMPT,
    PLAN_MODE_APPEND,
    AgentDef,
    get_agent,
    role_prompt_for_llm,
)
from voidx.agent.graph.compaction import GraphCompactionMixin
from voidx.agent.graph.convergence import (
    build_convergence_messages,
    generate_fallback_summary,
)
from voidx.agent.graph.permissions import GraphPermissionMixin
from voidx.agent.graph.runtime import (
    console,
    current_parent_tool_call_id as _current_parent_tool_call_id,
    ui,
)
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.topology import (
    build_graph,
    latest_ai_message,
    latest_user_text,
    pending_approval_scope,
    prepare_state,
    session_date,
)
from voidx.agent.graph.wiring import (
    bind_settings_to_catalog,
    build_compaction_service,
    build_external_managers,
    build_permission_service,
    build_tool_registry,
    register_agent_tool,
)
from voidx.agent.state import AgentState
from voidx.agent.graph.streaming import extract_text, stream_llm as _stream_llm
from voidx.agent.graph.subagent import run_subagent as _run_subagent
from voidx.agent.graph.tool_execution import GraphToolExecutionMixin
from voidx.agent.intent_refinement import refine_intent
from voidx.agent.runtime_context import ContextCompilerCache, InteractionMode, RuntimeContextBuilder
from voidx.agent.task_state import TaskRun, TaskState
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
from voidx.config import Config, Settings
from voidx.llm.instruction import InstructionService
from voidx.llm.provider import create_chat_model, resolve_protocol
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.usage import (
    estimate_context_tokens,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.memory.session import MessageRow, SessionInfo
from voidx.skills.runtime import SkillRunState
from voidx.tools.on_intent import OnIntentInput
from voidx.runtime.ui import (
    OutputNode,
    OutputTree,
    PureTui,
    GuidanceSubmitted,
    StreamingRenderer,
    SubagentFinished,
    SubagentStarted,
    dock,
    ui_events,
    via_events,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphComponentHost


GUIDANCE_MAX_CHARS = 2_000


def _restored_skill_runs(task_run: TaskRun | None) -> list[SkillRunState]:
    if task_run is None:
        return []
    return list((task_run.skill_runs or {}).values())


def _merge_skill_runs(*groups: list[SkillRunState | dict]) -> list[SkillRunState]:
    merged: dict[str, SkillRunState] = {}
    for group in groups:
        for item in group:
            try:
                run = item if isinstance(item, SkillRunState) else SkillRunState.model_validate(item)
            except ValueError:
                continue
            merged[run.name] = run
    return list(merged.values())


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

        bind_settings_to_catalog(settings)
        self._tracker, self.tools = build_tool_registry(
            settings=settings,
            config=config,
            on_intent_resolver=self._resolve_on_intent,
            subagent_runner=self._subagent_runner,
        )

        self._instruction = InstructionService(self._workspace, settings=settings)
        self._permission = build_permission_service(config, notifier=ui.print)

        self._interaction_mode: InteractionMode = InteractionMode.AUTO
        self._debug: bool = False
        ui.set_debug(self._debug)

        self._file_mtimes: dict[str, float] = {}
        self._turn_node: OutputNode | None = None
        self._current_tree: OutputTree | None = None
        self._current_messages: list[BaseMessage] | None = None
        self._sub_buffers: dict[str, list[BaseMessage]] = {}
        self._pending_summary: str | None = None
        self._compaction_summary: str = ""
        self._session_date: str = session_date(session)
        self._session_msg_cache: list[MessageRow] | None = None
        self._context_cache = ContextCompilerCache()
        self._app: PureTui | None = None
        self._next_agent_id: int = 0
        self._task_state = TaskState()
        self._task_run = TaskRun()
        self._needs_failure_check: dict[str, dict] = {}
        self._pending_guidance: list[str] = []
        self._usage_stats, self._compaction = build_compaction_service(config)

        self._build()
        from voidx.agent.slash import SlashHandler

        self._slash = SlashHandler(self)
        self._mcp_manager, self._lsp_manager = build_external_managers(
            settings=self._settings,
            tools=self.tools,
            permission=self._permission,
            workspace=self._workspace,
        )
        if TYPE_CHECKING:
            _host_contract: GraphComponentHost = self

    @property
    def app(self) -> PureTui | None:
        """The interactive TUI app, if one is running."""
        return self._app

    @property
    def permission(self):
        return self._permission

    @property
    def session(self) -> SessionInfo | None:
        return self._session

    @property
    def settings(self) -> Settings | None:
        return self._settings

    @property
    def task_run(self) -> TaskRun:
        return self._task_run

    @property
    def task_state(self) -> TaskState:
        return self._task_state

    @property
    def usage_stats(self):
        return self._usage_stats

    @property
    def workspace(self) -> str:
        return self._workspace

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

    def debug_enabled(self) -> bool:
        return self._debug

    def set_task_run(self, task_run: TaskRun) -> None:
        self._task_run = task_run

    def submit_guidance(self, text: str) -> bool:
        guidance = " ".join(text.strip().split())
        if not guidance:
            return False
        truncated = False
        if len(guidance) > GUIDANCE_MAX_CHARS:
            guidance = guidance[:GUIDANCE_MAX_CHARS].rstrip()
            truncated = True
        self._pending_guidance.append(guidance)
        if (
            not via_events()
            or not ui_events.emit_direct(GuidanceSubmitted(text=guidance, truncated=truncated))
        ):
            suffix = " [dim](truncated)[/dim]" if truncated else ""
            dock.append_message(f"[dim][guide][/dim] {guidance}{suffix}", markup=True)
        return True

    def _drain_pending_guidance(self) -> list[HumanMessage]:
        messages: list[HumanMessage] = []
        while self._pending_guidance:
            text = self._pending_guidance.pop(0)
            messages.append(HumanMessage(
                content=text,
                additional_kwargs={GUIDANCE_MARKER: True},
            ))
        return messages

    async def persist_runtime_state(self) -> None:
        await self._persist_runtime_state()

    async def compact_session_history(self, *, force: bool = True) -> bool:
        return await self._compact_session_history(force=force)

    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        return await self._restore_transcript_snapshot(append=append)

    async def show_startup(self, *, append_transcript: bool = False) -> None:
        await self._show_startup(append_transcript=append_transcript)

    async def clear_current_session(self) -> None:
        if self._session is None:
            return

        from voidx.memory.session import clear_messages, update_title

        await clear_messages(self._session.id)
        await update_title(self._session.id, "New session")
        await self._clear_runtime_state()
        self._session = self._session.model_copy(update={
            "title": "New session",
            "message_count": 0,
        })
        self._session_msg_cache = []
        self._context_cache = ContextCompilerCache()
        self._reload_parallel_subagents_from_settings()
        self._tracker.clear_todos()
        self._permission.clear_session_permissions()
        self._usage_stats.reset()

    def _reload_parallel_subagents_from_settings(self) -> None:
        if self._settings is None:
            return
        self.config.parallel_subagents = self._settings.get_parallel_subagents()
        register_agent_tool(
            self.tools,
            config=self.config,
            subagent_runner=self._subagent_runner,
        )

    async def resume_session(self, session: SessionInfo) -> None:
        self._session = session
        self._workspace = session.workspace
        self.config.workspace = session.workspace
        self._session_date = session_date(session)
        self._session_msg_cache = None
        self._context_cache = ContextCompilerCache()
        await self._restore_runtime_state()
        self._reload_parallel_subagents_from_settings()

    async def set_session_title(self, title: str) -> None:
        if self._session is None:
            return

        from voidx.memory.session import update_title

        await update_title(self._session.id, title)
        self._session = self._session.model_copy(update={"title": title})

    def _resolve_on_intent(self, inp: OnIntentInput, ctx):
        return refine_intent(
            inp,
            ctx,
            config=self.config,
            settings=self._settings,
            registered_tool_ids=self.tools.ids(),
        )

    async def _subagent_runner(self, agent_def: AgentDef, description: str, model_override: str | None) -> str:
        # Apply configured max_steps override
        agent_def = self._apply_max_steps_override(agent_def)
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

    def _apply_max_steps_override(self, agent_def: AgentDef) -> AgentDef:
        """Override agent_def.max_steps with configured value if present."""
        steps_map = getattr(self.config, 'agent_max_steps', None)
        if steps_map is None:
            return agent_def
        configured = getattr(steps_map, agent_def.name, None)
        if configured is not None:
            return agent_def.with_max_steps(configured)
        return agent_def

    def _build(self) -> None:
        self.graph = build_graph(self)

    # ── nodes ───────────────────────────────────────────────────────────

    async def _prepare_with_stream(self, state: AgentState) -> dict:
        base = prepare_state(state)
        agent_name = state.get("agent", "orchestrator")
        self._current_agent = self._apply_max_steps_override(get_agent(agent_name))
        role_prompt = (
            role_prompt_for_llm(
                self._current_agent,
                parallel_subagents_enabled=self.config.parallel_subagents.enabled,
            )
            if self._current_agent else ""
        )
        tool_contract = self._current_agent.tool_contract if self._current_agent else ""

        interaction_mode = state.get("interaction_mode") or (
            InteractionMode.PLAN.value if state.get("plan_mode", False) else self._interaction_mode.value
        )
        current_user_text = latest_user_text(state.get("messages", []))
        instructions = await self._instruction.system()
        skill_context = await self._instruction.skill_context_for(
            current_user_text,
            agent=agent_name,
            task_intent=state.get("task_intent"),
            interaction_mode=interaction_mode,
            scope=pending_approval_scope(state.get("pending_approval")) or state.get("goal") or current_user_text,
            turn_count=state.get("goal_turn_count", 0),
        )
        skill_runs = _merge_skill_runs(
            _restored_skill_runs(getattr(self, "_task_run", None)),
            state.get("skill_runs", []) or [],
            skill_context.runs,
        )
        mode_prompt = PLAN_MODE_APPEND if InteractionMode.parse(interaction_mode) == InteractionMode.PLAN else ""
        summary = self._pending_summary or self._compaction_summary
        self._pending_summary = None

        context, self._context_cache = RuntimeContextBuilder(
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
            skill_runs=skill_runs,
            active_skill_summaries=skill_context.active,
            summary=summary,
            current_user_text=current_user_text,
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
        ).build_incremental(self._context_cache)
        context.apply_to_messages(state.get("messages", []))

        return {**base, "skill_runs": skill_runs}

    async def _call_llm(self, state: AgentState) -> dict:
        step = state.get("step_count", 0)
        max_s = state.get("max_steps", 100)  # fallback: orchestrator default
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
        guidance_messages = self._drain_pending_guidance()
        base_messages = [*state["messages"], *guidance_messages]
        convergence_messages, convergence_forced = build_convergence_messages(
            step=step,
            max_steps=max_s,
            has_tool_budget=has_tool_budget,
            goal=state.get("goal", "") or latest_user_text(base_messages),
        )
        llm_messages = [*base_messages, *convergence_messages]

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
            "messages": [*guidance_messages, assistant_msg],
            "step_count": step + 1,
            "convergence_forced": convergence_forced,
        }

    def _router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            if state.get("step_count", 0) >= state.get("max_steps", 100):  # fallback: orchestrator default
                return "end"
            return "execute"
        return "end"

    async def _finalize(self, state: AgentState) -> dict:
        if not state.get("convergence_forced"):
            return {}
        last = latest_ai_message(state.get("messages", []))
        if isinstance(last, AIMessage) and not last.tool_calls:
            if len(extract_text(last).strip()) >= 20:
                return {}
        return {"messages": [AIMessage(content=generate_fallback_summary(state))]}
