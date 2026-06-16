"""Agent graph — LangGraph state machine.

voidx uses one primary agent identity (`voidx`) and runtime thinking-mode
personas (`coordinate`, `explore`, `plan`, `implement`, `review`).

Depth limit = 1: child agents cannot start further child agents.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

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
    persona_prompt_for_llm,
)
from voidx.agent.graph.compaction import GraphCompactionMixin
from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator
from voidx.agent.graph.convergence import generate_fallback_summary
from voidx.agent.graph.permissions import GraphPermissionMixin
from voidx.agent.graph.runtime import current_parent_tool_call_id as _current_parent_tool_call_id
from voidx.agent.graph.runtime_guards import RuntimeGuardState
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.session_runtime import GraphSessionRuntime
from voidx.agent.graph.topology import (
    build_graph,
    latest_ai_message,
    latest_user_text,
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
from voidx.logging.request_log import log_llm_exchange
from voidx.agent.graph.subagent import run_subagent as _run_subagent
from voidx.agent.graph.title_mixin import GraphTitleMixin
from voidx.agent.todo_state import apply_todo_state_to_host, sanitize_todo_replay_messages
from voidx.agent.graph.tool_executor import GraphToolExecutor
from voidx.agent.graph.tool_execution import GraphToolExecutionMixin
from voidx.agent.graph.turn_runner import GraphTurnRunner
from voidx.agent.runtime_context import (
    COMPACTION_GUIDE_MARKER,
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
    current_todo_context_message,
    raw_semantic_messages,
)
from voidx.agent.task_state import GoalResolution, TaskState, goal_label, goal_type_value
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
from voidx.config import Config, Settings
from voidx.llm.instruction import InstructionService
from voidx.llm.service import create_chat_model, resolve_protocol
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.usage import (
    estimate_context_tokens,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.memory.service import (
    MessageRow,
    SessionInfo,
    append_subagent_event,
    save_context_frame_from_messages,
)
from voidx.runtime.ui import (
    GuidanceSubmitted,
    OutputNode,
    OutputTree,
    PureTui,
    StreamingRenderer,
    StatusFinished,
    StatusUpdated,
    SubagentFinished,
    SubagentStarted,
)
from voidx.runtime.ui_port import runtime_ui_port
from voidx.skills.service import SkillRegistry, SkillService
from voidx.workflow import workflow_personas
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus

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


def _render_inline_compaction_guide(*, tail_anchor_id: str, head_count: int, previous_summary: str) -> str:
    previous = previous_summary.strip() or "(none)"
    return (
        f"{COMPACTION_GUIDE_MARKER}\n"
        "Scope: inline-context-compaction\n\n"
        "The conversation is large enough to compact older context without a separate compaction request.\n"
        "If you can preserve the durable facts now, call compact_context before continuing.\n\n"
        "Rules:\n"
        "- Summarize only older context before the tail anchor.\n"
        "- Preserve durable facts, decisions, constraints, changed files, verification results, blockers, and next steps.\n"
        "- Drop transient narration, repeated tool outputs, and stale execution detail.\n"
        "- Do not answer the user through compact_context; use it only to update runtime memory.\n"
        "- After compact_context succeeds, continue with the user's request normally.\n\n"
        "Current compaction request:\n"
        f"- tail_anchor_id: {tail_anchor_id}\n"
        f"- older_messages_to_summarize: {head_count}\n"
        f"- previous_summary:\n{previous}"
    )


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


def _persona_for_workflow_runs(
    group: list[WorkflowRunState | dict],
    *,
    fallback: str = "coordinate",
) -> str:
    personas: list[str] = []
    for item in group:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        personas.extend(persona.strip() for persona in run.personas if persona.strip())
    if not personas:
        return fallback or "coordinate"
    return ",".join(dict.fromkeys(personas))


def _persona_for_child_workflow(group: list[WorkflowRunState | dict], join: str) -> str:
    persona = _persona_for_workflow_runs(group, fallback="")
    if persona:
        return persona
    personas = [item for item in workflow_personas(join) if item.strip()]
    return ",".join(dict.fromkeys(personas)) or "explore"


def _interaction_mode_for_persona(persona: str) -> str:
    personas = {item.strip() for item in persona.split(",") if item.strip()}
    return InteractionMode.PLAN.value if "plan" in personas else InteractionMode.AUTO.value


def _subagent_step_budget(goal_resolution: GoalResolution) -> int:
    plan = goal_resolution.plan
    join = (plan.join if plan is not None else "").strip().lower()
    return {
        "review": 4,
        "verify": 4,
        "plan": 5,
        "design-doc": 5,
        "debug": 6,
        "tdd": 6,
        "feedback": 6,
    }.get(join, 5)


def _invalidate_tui(host: object) -> None:
    app = getattr(host, "_app", None)
    invalidate = getattr(app, "invalidate", None)
    if callable(invalidate):
        invalidate()


def _agent_static_tool_defs(agent: AgentDef | None, all_tool_defs: list[dict]) -> list[dict]:
    """Apply AgentDef's static tool catalog visibility.

    This is not runtime persona/workflow policy. Runtime policy is enforced by
    the tool-engine during authorization; this only prevents tools outside the
    current agent identity's declared catalog from being advertised to the LLM.
    """
    if agent is None:
        return all_tool_defs
    agent_tool_ids = set(agent.tools)
    mcp_allowed = bool(agent.mcp_tools)
    return [
        tool_def
        for tool_def in all_tool_defs
        if (
            tool_def["function"]["name"] in agent_tool_ids
            or (mcp_allowed and tool_def["function"]["name"].startswith("mcp__"))
        )
    ]


def _task_state_for_context(value: object, fallback: TaskState | None = None) -> TaskState:
    if isinstance(value, TaskState):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        try:
            return TaskState.model_validate(value)
        except ValueError:
            pass
    if fallback is not None:
        return fallback.model_copy(deep=True)
    return TaskState()


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
        self._needs_failure_check: dict[str, dict] = {}
        self._runtime_guards = RuntimeGuardState()
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

        from voidx.runtime.ui import ToolDisplayPolicy, DEFAULT_DISPLAY_RULES
        display_config = getattr(config, "display_policy", None) or {}
        self._display_policy = ToolDisplayPolicy.from_config(display_config, defaults=DEFAULT_DISPLAY_RULES)

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

    def set_task_state(self, task_state: TaskState) -> None:
        self._task_state = task_state

    def submit_guidance(self, text: str) -> bool:
        guidance = " ".join(text.strip().split())
        if not guidance:
            return False
        truncated = False
        if len(guidance) > GUIDANCE_MAX_CHARS:
            guidance = guidance[:GUIDANCE_MAX_CHARS].rstrip()
            truncated = True
        self._pending_guidance.append(guidance)
        if self._ui.via_events():
            self._ui.events.emit_direct(GuidanceSubmitted(text=guidance, truncated=truncated))
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
        from voidx.memory.service import clear_messages, update_title

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

        from voidx.memory.service import update_title

        self._invalidate_session_title_generation()
        await update_title(self._session.id, title)
        self._session = self._session.model_copy(update={"title": title})

    async def _subagent_runner(
        self,
        agent_def: AgentDef,
        description: str,
        model_override: str | None,
        goal_resolution: GoalResolution,
        result_contract: Any,
    ) -> str:
        sub_buffer: list[BaseMessage] = []
        session_id = self._session.id if self._session else "default"
        agent_id = self._next_agent_id
        self._next_agent_id += 1
        parent_tool_call_id = _current_parent_tool_call_id.get()
        agent_run_id = f"agent_{agent_id}"
        started_at = time.monotonic()
        goal = goal_resolution.goal
        plan = goal_resolution.plan
        workflow_start = plan.join if plan is not None else ""
        goal_type = goal.type.value if goal is not None else ""
        workflow_runtime_context = await self._workflow_context_for(
            description,
            agent="",
            task_intent=goal_resolution.intent.type.value,
            goal_type=goal_type,
            interaction_mode=InteractionMode.AUTO.value,
            scope=goal.label if goal is not None else description,
            workflow_start=workflow_start,
        )
        runtime_persona = _persona_for_child_workflow(workflow_runtime_context.runs, workflow_start)
        interaction_mode = _interaction_mode_for_persona(runtime_persona)
        max_steps = _subagent_step_budget(goal_resolution)

        async def authorize(calls):
            return await self._authorize_tool_calls(
                calls,
                runtime_persona=runtime_persona,
                plan_mode=InteractionMode.parse(interaction_mode) == InteractionMode.PLAN,
                session_id=session_id,
                interaction_mode=interaction_mode,
                workflow_runs=workflow_runtime_context.runs,
            )

        if self._ui.via_events():
            await self._ui.events.emit(SubagentStarted(
                agent_id=agent_id,
                subagent_id=agent_run_id,
                name=runtime_persona,
                description=description,
                parent_agent_id=-1,
                parent_tool_call_id=parent_tool_call_id,
            ))
        if self._session:
            await append_subagent_event(session_id, agent_run_id, {
                "type": "subagent_start",
                "agent_id": agent_id,
                "persona": runtime_persona,
                "description": description,
                "parent_agent_id": -1,
                "parent_tool_call_id": parent_tool_call_id,
                "max_steps": max_steps,
                "goal_resolution": goal_resolution.model_dump(mode="json"),
                "result_schema": result_contract.schema_name,
            })

        ok = False
        run_metadata: dict[str, object] = {}
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
                "run_metadata": run_metadata,
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
                runtime_persona=runtime_persona,
                goal_resolution=goal_resolution,
                result_contract=result_contract,
                step_budget=max_steps,
                **kwargs,
            )
            ok = True
            return result
        finally:
            if self._ui.via_events():
                await self._ui.events.emit(SubagentFinished(
                    agent_id=agent_id,
                    subagent_id=agent_run_id,
                    ok=ok,
                    elapsed=time.monotonic() - started_at,
                    final_step=run_metadata.get("final_step") if ok else None,
                    max_steps=run_metadata.get("max_steps") if ok else max_steps,
                    finish_reason=str(run_metadata.get("finish_reason") or ("final_answer" if ok else "error")),
                ))
            if self._session:
                await append_subagent_event(session_id, agent_run_id, {
                    "type": "subagent_finish",
                    "agent_id": agent_id,
                    "ok": ok,
                    "elapsed": time.monotonic() - started_at,
                    "final_step": run_metadata.get("final_step") if ok else None,
                    "max_steps": run_metadata.get("max_steps") if ok else max_steps,
                    "finish_reason": str(run_metadata.get("finish_reason") or ("final_answer" if ok else "error")),
                })

    def set_debug(self, value: bool) -> None:
        self._debug = value
        self._instruction.set_debug(value)
        self._ui.ui.set_debug(value)

    def _build(self) -> None:
        self.graph = build_graph(self)

    # ── nodes ───────────────────────────────────────────────────────────

    async def _prepare_with_stream(self, state: AgentState) -> dict:
        base = prepare_state(state)
        agent_id = "voidx"
        runtime_persona = state.get("persona", "coordinate")
        self._current_agent = get_agent(agent_id)
        persona_prompt = (
            persona_prompt_for_llm(
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
        task_state = _task_state_for_context(state.get("task_state"), getattr(self, "_task_state", None))
        current_goal = task_state.current_goal
        existing_workflow_runs = list((task_state.workflow_runs or {}).values())
        workflow_start = (
            task_state.workflow_route.join
            if task_state.workflow_route and task_state.workflow_route.join
            else None
        )
        workflow_context = await self._workflow_context_for(
            current_user_text,
            agent=runtime_persona,
            task_intent=task_state.current_intent.value,
            goal_type=goal_type_value(current_goal),
            interaction_mode=interaction_mode,
            scope=goal_label(current_goal) or current_user_text,
            exclude_names=_workflow_names(existing_workflow_runs),
            active_names=_active_workflow_names(existing_workflow_runs),
            workflow_start=workflow_start,
        )
        workflow_runs = _merge_workflow_runs(
            existing_workflow_runs,
            workflow_context.runs,
        )
        runtime_persona = _persona_for_workflow_runs(workflow_runs, fallback=runtime_persona)
        mode_prompt = PLAN_MODE_APPEND if InteractionMode.parse(interaction_mode) == InteractionMode.PLAN else ""
        summary = self._pending_summary or self._compaction_summary
        self._pending_summary = None

        context, self._context_cache = RuntimeContextBuilder(
            config=self.config,
            workspace=state.get("workspace", "."),
            base_system_prompt=BASE_SYSTEM_PROMPT,
            persona_prompt=persona_prompt,
            mode_prompt=mode_prompt,
            tool_contract=tool_contract,
            persona=runtime_persona,
            interaction_mode=interaction_mode,
            instructions=instructions,
            workflow_context_content=workflow_context.content,
            workflow_runs=workflow_runs,
            active_workflow_summaries=workflow_context.active,
            summary=summary,
            current_user_text=current_user_text,
            task_state=task_state,
            session_date=self._session_date,
            include_goal_resolution_guide=state.get("step_count", 0) == 0,
        ).build_incremental(self._context_cache)
        context.apply_to_messages(state.get("messages", []))

        task_state.workflow_runs = {run.name: run for run in workflow_runs}
        self._task_state = task_state.model_copy(deep=True)
        _invalidate_tui(self)
        return {
            **base,
            "persona": runtime_persona,
            "task_state": task_state.model_dump(mode="json"),
        }

    async def _workflow_context_for(self, *args, **kwargs):
        return await self._instruction.workflow_context_for(*args, **kwargs)

    def _inline_compaction_guide_for(self, messages: list[BaseMessage]) -> HumanMessage | None:
        total_tokens = estimate_context_tokens(messages, self.config.model.model)
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}
        if self._compaction.is_overflow(tokens):
            return None
        if total_tokens < self._compaction.usable_window():
            return None

        semantic_messages = sanitize_todo_replay_messages(raw_semantic_messages(messages))
        selection = self._compaction.select_details(semantic_messages)
        if not selection.should_compact:
            return None
        content = _render_inline_compaction_guide(
            tail_anchor_id=selection.tail_id or "",
            head_count=len(selection.head),
            previous_summary=self._compaction_summary,
        )
        guide = HumanMessage(content=content)
        guide_tokens = estimate_context_tokens([*messages, guide], self.config.model.model)
        guide_budget = {"total": guide_tokens, "input": guide_tokens, "output": 0, "reasoning": 0}
        if guide_tokens > self._compaction.context_limit or self._compaction.is_overflow(guide_budget):
            return None
        return guide

    async def _call_llm(self, state: AgentState) -> dict:
        step = state.get("step_count", 0)

        if self.model is None:
            return {
                "messages": [AIMessage(content=(
                    "No model configured. Use /model new to create a profile."
                ))],
                "step_count": step,
                "should_continue": False,
            }

        agent = get_agent("voidx")
        tool_defs = _agent_static_tool_defs(agent, self.tools.tools_for_llm())
        runtime_task_state = _task_state_for_context(
            state.get("task_state"),
            getattr(self, "_task_state", None),
        )
        tool_defs = filter_unavailable_lsp_tools(tool_defs, getattr(self, "_lsp_manager", None))

        guidance_messages = self._drain_pending_guidance()
        state_messages = sanitize_todo_replay_messages(
            list(state["messages"]),
            preserve_latest_tool_exchange=True,
        )
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
            inline_compaction_guide = self._inline_compaction_guide_for(base_messages)
            if inline_compaction_guide is not None:
                base_messages.append(inline_compaction_guide)
            return base_messages, [], False

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
                agent_persona=persona,
                provider=self.config.model.provider,
                model=self.config.model.model,
                messages=messages,
                token_estimate=token_estimate,
                metadata={
                    "step": step,
                    "tool_count": len(tool_defs),
                    "convergence_hint_count": len(convergence_messages),
                    "convergence_forced": convergence_forced,
                },
            )

        def replacement_messages(assistant_msg: AIMessage) -> list[BaseMessage]:
            if not compaction_happened:
                return [assistant_msg]
            return [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *state_messages,
                assistant_msg,
            ]

        llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(state_messages)

        persona = state.get("persona", "coordinate")
        if self._debug:
            self._ui.ui.print()

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
        retry_status_active = False
        while True:
            try:
                renderer = StreamingRenderer(self._ui.console, debug=self._debug)
                model_with_tools = self.model.bind_tools(tool_defs) if tool_defs else self.model
                assistant_msg = await _stream_llm(model_with_tools, llm_messages, renderer, resolve_protocol(self.config.model))
                log_llm_exchange(
                    llm_messages,
                    assistant_msg,
                    model=self.config.model.model,
                    provider=self.config.model.provider,
                    step=step,
                    session_id=self._session.id if self._session else None,
                )
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
                if retry_status_active and self._ui.via_events():
                    await self._ui.events.emit(StatusFinished(status_id="llm:retry"))
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
                    if self._ui.via_events():
                        retry_status_active = True
                        await self._ui.events.emit(StatusUpdated(
                            status_id="llm:retry",
                            label=f"LLM error, retrying in {delay}s",
                            detail=str(e),
                        ))
                    else:
                        self._ui.ui.print(f"[dim]LLM error, retrying in {delay}s: {e}[/dim]")
                    await asyncio.sleep(delay)
                else:
                    if retry_status_active and self._ui.via_events():
                        await self._ui.events.emit(StatusFinished(status_id="llm:retry"))
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
