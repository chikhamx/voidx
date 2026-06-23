"""Agent graph — LangGraph state machine.

voidx uses one primary agent identity (`voidx`) and runtime thinking-mode
personas (`coordinate`, `explore`, `plan`, `implement`, `review`).

Depth limit = 1: child agents cannot start further child agents.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import BaseMessage, HumanMessage

from voidx.agent.agents import AgentDef, get_agent
from voidx.agent.graph.compaction import GraphCompactionMixin
from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator
from voidx.agent.graph.convergence import generate_fallback_summary
from voidx.agent.graph.core.helpers import (
    _interaction_mode_for_persona,
    _invalidate_tui,
    _persona_for_child_workflow,
)
from voidx.agent.graph.core.llm import GraphLlmMixin
from voidx.agent.graph.permissions import GraphPermissionMixin
from voidx.agent.graph.runtime import current_parent_tool_call_id as _current_parent_tool_call_id
from voidx.agent.graph.runtime_guards import RuntimeGuardState
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.session_runtime import GraphSessionRuntime
from voidx.agent.graph.streaming import stream_llm as _stream_llm
from voidx.agent.graph.subagent import run_subagent as _run_subagent
from voidx.agent.graph.title_mixin import GraphTitleMixin
from voidx.agent.graph.tool_executor import GraphToolExecutor
from voidx.agent.graph.tool_execution import GraphToolExecutionMixin
from voidx.agent.graph.topology import build_graph, session_date
from voidx.agent.graph.turn_runner import GraphTurnRunner
from voidx.agent.graph.wiring import (
    bind_settings_to_catalog,
    build_compaction_service,
    build_external_managers,
    build_permission_service,
    build_tool_registry,
    register_agent_tool,
)
from voidx.agent.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
)
from voidx.agent.task_state import GoalResolution, TaskState, goal_type_from_join
from voidx.agent.todo_state import apply_todo_state_to_host
from voidx.config import Config, Settings
from voidx.llm.instruction import InstructionService
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.service import create_chat_model
from voidx.memory.service import SessionInfo, append_subagent_event
from voidx.runtime.ui import (
    GuidanceSubmitted,
    OutputNode,
    OutputTree,
    PureTui,
    SubagentFinished,
    SubagentStarted,
)
from voidx.runtime.ui_port import runtime_ui_port
from voidx.skills.service import SkillRegistry, SkillService

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphComponentHost


GUIDANCE_MAX_CHARS = 2_000


class VoidXGraph(
    GraphTitleMixin,
    GraphRunLoopMixin,
    GraphCompactionMixin,
    GraphToolExecutionMixin,
    GraphPermissionMixin,
    GraphLlmMixin,
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

        self._file_mtimes: dict[str, dict[str, int]] = {}
        self._file_read_coverage: dict[str, dict] = {}
        self._turn_node: OutputNode | None = None
        self._current_tree: OutputTree | None = None
        self._current_messages: list[BaseMessage] | None = None
        self._pending_summary: str | None = None
        self._compaction_summary: str = ""
        self._session_date: str = session_date(session)
        self._session_msg_cache: list | None = None
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

    @property
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
        goal_type = goal_type_from_join(workflow_start)
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
                    finish_reason=str(run_metadata.get("finish_reason") or ("final_answer" if ok else "error")),
                ))
            if self._session:
                await append_subagent_event(session_id, agent_run_id, {
                    "type": "subagent_finish",
                    "agent_id": agent_id,
                    "ok": ok,
                    "elapsed": time.monotonic() - started_at,
                    "finish_reason": str(run_metadata.get("finish_reason") or ("final_answer" if ok else "error")),
                })

    def set_debug(self, value: bool) -> None:
        self._debug = value
        self._instruction.set_debug(value)
        self._ui.ui.set_debug(value)

    def _build(self) -> None:
        self.graph = build_graph(self)
