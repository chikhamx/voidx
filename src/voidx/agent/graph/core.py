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
    RemoveMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from voidx.agent.agents import (
    BASE_SYSTEM_PROMPT,
    PLAN_MODE_APPEND,
    AgentDef,
    get_agent,
    role_prompt_for_llm,
)
from voidx.agent.graph.compaction import GraphCompactionMixin
from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator
from voidx.agent.graph.convergence import (
    build_convergence_messages,
    generate_fallback_summary,
)
from voidx.agent.graph.permissions import GraphPermissionMixin
from voidx.agent.graph.runtime import current_parent_tool_call_id as _current_parent_tool_call_id
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.session_runtime import GraphSessionRuntime
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
from voidx.agent.graph.subagent import _task_intent_for_agent as _subagent_task_intent_for_agent
from voidx.agent.graph.subagent import run_subagent as _run_subagent
from voidx.agent.graph.title_mixin import GraphTitleMixin
from voidx.agent.todo_state import apply_todo_state_to_host, sanitize_todo_replay_messages
from voidx.agent.graph.tool_executor import GraphToolExecutor
from voidx.agent.graph.tool_execution import GraphToolExecutionMixin
from voidx.agent.graph.turn_runner import GraphTurnRunner
from voidx.agent.intent_refinement import refine_intent
from voidx.agent.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
    current_todo_context_message,
)
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
from voidx.runtime.ui_port import runtime_ui_port
from voidx.skills.registry import SkillRegistry
from voidx.skills.service import SkillService
from voidx.tools.on_intent import OnIntentInput
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.events.schema import (
    GuidanceSubmitted,
    SubagentFinished,
    SubagentStarted,
)
from voidx.ui.output.tree import OutputNode, OutputTree
from voidx.ui.tui import PureTui
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphComponentHost


GUIDANCE_MAX_CHARS = 2_000


def _is_context_overflow_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        pattern in msg
        for pattern in (
            "context_length_exceeded",
            "context length",
            "too many tokens",
            "maximum context",
            "token limit",
            "input is too long",
            "request too large",
            "context window",
        )
    )


def _restored_workflow_runs(task_run: TaskRun | None) -> list[WorkflowRunState]:
    if task_run is None:
        return []
    return list((task_run.workflow_runs or {}).values())


def _merge_workflow_runs(*groups: list[WorkflowRunState | dict]) -> list[WorkflowRunState]:
    merged: dict[str, WorkflowRunState] = {}
    for group in groups:
        for item in group:
            try:
                run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
            except ValueError:
                continue
            merged[run.name] = run
    return list(merged.values())


def _workflow_names(group: list[WorkflowRunState | dict]) -> list[str]:
    names: list[str] = []
    for item in group:
        if isinstance(item, WorkflowRunState):
            name = item.name
        elif isinstance(item, dict):
            name = item.get("name", "")
        else:
            name = ""
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _active_workflow_names(group: list[WorkflowRunState | dict]) -> list[str]:
    names: list[str] = []
    for item in group:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        if run.status == WorkflowRunStatus.ACTIVE and run.name.strip():
            names.append(run.name.strip())
    return names


class VoidXGraph(
    GraphTitleMixin,
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
        self._ui = runtime_ui_port

        bind_settings_to_catalog(settings)
        self._tracker, self.tools = build_tool_registry(
            settings=settings,
            config=config,
            on_intent_resolver=self._resolve_on_intent,
            subagent_runner=self._subagent_runner,
        )

        self._instruction = InstructionService(self._workspace, settings=settings)
        self._permission = build_permission_service(config, notifier=self._ui.ui.print)

        self._interaction_mode: InteractionMode = InteractionMode.AUTO
        self._debug: bool = False
        self._instruction.set_debug(self._debug)
        self._ui.ui.set_debug(self._debug)

        self._file_mtimes: dict[str, float] = {}
        self._turn_node: OutputNode | None = None
        self._current_tree: OutputTree | None = None
        self._current_messages: list[BaseMessage] | None = None
        self._pending_summary: str | None = None
        self._compaction_summary: str = ""
        self._in_turn_compaction_count: int = 0
        self._session_date: str = session_date(session)
        self._session_msg_cache: list[MessageRow] | None = None
        self._context_cache = ContextCompilerCache()
        self._app: PureTui | None = None
        self._next_agent_id: int = 0
        self._task_state = TaskState()
        self._task_run = TaskRun()
        self._needs_failure_check: dict[str, dict] = {}
        self._pending_guidance: list[str] = []
        self._clear_session_tasks: set[asyncio.Task[None]] = set()
        self._title_generation: int = 0
        self._title_task: asyncio.Task[None] | None = None
        self._usage_stats, self._compaction = build_compaction_service(config)
        self._compaction_coordinator = GraphCompactionCoordinator(self)
        self._session_runtime = GraphSessionRuntime(self)
        self._tool_executor = GraphToolExecutor(self)
        self._turn_runner = GraphTurnRunner(self)
        self._skill_service: SkillService | None = None

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

    def _skill_service_for_references(self) -> SkillService:
        if self._skill_service is None:
            settings = self._settings or Settings(self._workspace)
            self._skill_service = SkillService(
                SkillRegistry(self._workspace),
                selection=settings.get_skill_selection(),
            )
        return self._skill_service

    def _invalidate_skill_service_cache(self) -> None:
        self._skill_service = None
        app = getattr(self, "_app", None)
        invalidate = getattr(app, "invalidate_skill_service_cache", None)
        if callable(invalidate):
            invalidate()

    @property
    def usage_stats(self):
        return self._usage_stats

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def mcp_manager(self):
        return getattr(self, "_mcp_manager", None)

    @property
    def lsp_manager(self):
        return getattr(self, "_lsp_manager", None)

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
            not self._ui.via_events()
            or not self._ui.events.emit_direct(GuidanceSubmitted(text=guidance, truncated=truncated))
        ):
            suffix = " [dim](truncated)[/dim]" if truncated else ""
            self._ui.dock.append_message(f"[dim][guide][/dim] {guidance}{suffix}", markup=True)
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

    async def show_startup(
        self,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> None:
        await self._show_startup(
            append_transcript=append_transcript,
            prefer_direct=prefer_direct,
        )

    async def run_synthetic_turn(self, text: str, *, display_text: str | None = None) -> None:
        await self._run_once(text, display_text=display_text)

    async def clear_current_session(self) -> None:
        self._invalidate_session_title_generation()
        old_session_id = self._session.id if self._session is not None else None
        self._session = None
        self._session_date = session_date(None)
        self._session_msg_cache = []
        self._context_cache = ContextCompilerCache()
        self._reset_runtime_state_memory()
        self._reload_parallel_subagents_from_settings()
        self._tracker.clear_todos()
        self._permission.clear_session_permissions()
        self._usage_stats.reset()
        self._current_messages = None
        self._pending_guidance.clear()
        if old_session_id:
            self._schedule_clear_session_storage(old_session_id)

    def _schedule_clear_session_storage(self, session_id: str) -> None:
        task = asyncio.create_task(
            self._clear_session_storage(session_id),
            name=f"voidx-clear-session-{session_id}",
        )
        self._clear_session_tasks.add(task)
        task.add_done_callback(self._clear_session_tasks.discard)

    async def _clear_session_storage(self, session_id: str) -> None:
        from voidx.memory.session import clear_messages, update_title

        try:
            await clear_messages(session_id)
            await update_title(session_id, "New session", touch=False)
        except Exception as exc:
            self._ui.ui.print(f"[red]Clear cleanup failed: {exc}[/red]")

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
        self._invalidate_session_title_generation()
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

        self._invalidate_session_title_generation()
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
        sub_buffer: list[BaseMessage] = []
        session_id = self._session.id if self._session else "default"
        agent_id = self._next_agent_id
        self._next_agent_id += 1
        parent_tool_call_id = _current_parent_tool_call_id.get()
        started_at = time.monotonic()
        interaction_mode = InteractionMode.PLAN.value if agent_def.name == "plan" else InteractionMode.AUTO.value
        task_intent = _subagent_task_intent_for_agent(agent_def.name)
        workflow_runtime_context = await self._workflow_context_for(
            description,
            agent=agent_def.name,
            task_intent=task_intent,
            interaction_mode=interaction_mode,
            scope=description,
            turn_count=getattr(getattr(self, "_task_run", None), "turn_count", 0),
        )

        async def authorize(calls, agent_name: str):
            return await self._authorize_tool_calls(
                calls,
                agent_name=agent_name,
                plan_mode=InteractionMode.parse(interaction_mode) == InteractionMode.PLAN,
                session_id=session_id,
                interaction_mode=interaction_mode,
                workflow_runs=workflow_runtime_context.runs,
            )

        if self._ui.via_events():
            await self._ui.events.emit(SubagentStarted(
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
                "sub_messages": sub_buffer,
                "authorize_tools": authorize,
                "debug": self._debug,
                "agent_id": agent_id,
                "session_id": session_id if self._session else None,
                "usage_stats": self._usage_stats,
                "lsp_manager": getattr(self, "_lsp_manager", None),
                "parent_tools": self.tools,
                "workflow_runtime_context": workflow_runtime_context,
                "todo_state_sink": lambda todo_state: apply_todo_state_to_host(self, todo_state),
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
            return result
        finally:
            if self._ui.via_events():
                await self._ui.events.emit(SubagentFinished(
                    agent_id=agent_id,
                    subagent_id=f"agent_{agent_id}",
                    ok=ok,
                    elapsed=time.monotonic() - started_at,
                ))

    def set_debug(self, value: bool) -> None:
        self._debug = value
        self._instruction.set_debug(value)
        self._ui.ui.set_debug(value)

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
        existing_workflow_runs = _merge_workflow_runs(
            _restored_workflow_runs(getattr(self, "_task_run", None)),
            state.get("workflow_runs", []) or [],
        )
        workflow_context = await self._workflow_context_for(
            current_user_text,
            agent=agent_name,
            task_intent=state.get("task_intent"),
            interaction_mode=interaction_mode,
            scope=pending_approval_scope(state.get("pending_approval")) or state.get("goal") or current_user_text,
            turn_count=state.get("goal_turn_count", 0),
            exclude_names=_workflow_names(existing_workflow_runs),
            active_names=_active_workflow_names(existing_workflow_runs),
        )
        workflow_runs = _merge_workflow_runs(
            existing_workflow_runs,
            workflow_context.runs,
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
            skill_context_content=workflow_context.content,
            workflow_runs=workflow_runs,
            active_workflow_summaries=workflow_context.active,
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

        return {**base, "workflow_runs": workflow_runs}

    async def _workflow_context_for(self, *args, **kwargs):
        return await self._instruction.workflow_context_for(*args, **kwargs)

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
        agent_tool_ids = set(agent.tools) if agent else None
        mcp_allowed = bool(agent and agent.mcp_tools)
        all_tool_defs = self.tools.tools_for_llm()

        # Filter tools based on agent's allowlist
        if agent_tool_ids is not None:
            tool_defs = [
                t for t in all_tool_defs
                if (
                    t["function"]["name"] in agent_tool_ids
                    or (mcp_allowed and t["function"]["name"].startswith("mcp__"))
                )
            ]
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
        state_messages = sanitize_todo_replay_messages(list(state["messages"]))
        compaction_happened = False
        raw_todo_state = (
            state["todo_state"]
            if "todo_state" in state
            else getattr(getattr(self, "_task_state", None), "todo_state", None)
        )
        todo_context_message = current_todo_context_message(raw_todo_state)

        def rebuild_llm_messages(
            messages: list[BaseMessage],
        ) -> tuple[list[BaseMessage], list[HumanMessage], bool]:
            base_messages = [*messages, *guidance_messages]
            if todo_context_message is not None:
                base_messages.append(todo_context_message)
            convergence_messages, convergence_forced = build_convergence_messages(
                step=step,
                max_steps=max_s,
                has_tool_budget=has_tool_budget,
                goal=state.get("goal", "") or latest_user_text(base_messages),
            )
            return [*base_messages, *convergence_messages], convergence_messages, convergence_forced

        async def save_context_frame(
            messages: list[BaseMessage],
            token_estimate: int,
            convergence_messages: list[HumanMessage],
            convergence_forced: bool,
        ) -> None:
            if self._session is None:
                return
            await save_context_frame_from_messages(
                session_id=self._session.id,
                user_message_id=state.get("user_message_id"),
                frame_kind="main",
                agent_role=agent_name,
                provider=self.config.model.provider,
                model=self.config.model.model,
                messages=messages,
                token_estimate=token_estimate,
                metadata={
                    "step": step,
                    "max_steps": max_s,
                    "tool_count": len(tool_defs),
                    "convergence_hint_count": len(convergence_messages),
                    "convergence_forced": convergence_forced,
                },
            )

        def replacement_messages(assistant_msg: AIMessage) -> list[BaseMessage]:
            if not compaction_happened:
                return [*guidance_messages, assistant_msg]
            return [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *state_messages,
                *guidance_messages,
                assistant_msg,
            ]

        llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(state_messages)

        agent_name = state.get("agent", "orchestrator")
        if self._debug:
            self._ui.ui.print()
        self._ui.ui.step_header(step, max_s, agent_name)

        # ── LLM call with retry ────────────────────────────────────────
        context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
        self._usage_stats.update_context(context_tokens)
        if self._compaction.is_overflow({"total": context_tokens}):
            result = await self._in_turn_compact(state_messages)
            if result is not None:
                compaction_happened = True
                state_messages = list(result.live_messages)
                llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(state_messages)
                context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
                self._usage_stats.update_context(context_tokens)

        await save_context_frame(llm_messages, context_tokens, convergence_messages, convergence_forced)
        max_retries = 2
        failed_attempts = 0
        while True:
            try:
                renderer = StreamingRenderer(self._ui.console, debug=self._debug)
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
                    self._ui.ui.print()
                break
            except Exception as e:
                if _is_context_overflow_error(e):
                    result = await self._in_turn_compact(state_messages)
                    if result is not None:
                        compaction_happened = True
                        state_messages = list(result.live_messages)
                        llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(state_messages)
                        context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
                        self._usage_stats.update_context(context_tokens)
                        await save_context_frame(
                            llm_messages,
                            context_tokens,
                            convergence_messages,
                            convergence_forced,
                        )
                        continue
                if failed_attempts < max_retries:
                    failed_attempts += 1
                    delay = failed_attempts * 2
                    self._ui.ui.print(f"[dim]LLM error, retrying in {delay}s: {e}[/dim]")
                    await asyncio.sleep(delay)
                else:
                    self._ui.ui.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
                    failure_msg = AIMessage(content=f"LLM call failed: {e}")
                    return {
                        "messages": replacement_messages(failure_msg),
                        "step_count": step,
                        "should_continue": False,
                    }

        return {
            "messages": replacement_messages(assistant_msg),
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
