"""Agent graph — LangGraph state machine.

voidx uses one primary agent identity (`voidx`) and runtime thinking-mode
personas (`coordinate`, `explore`, `plan`, `implement`, `review`).

Depth limit = 1: child agents cannot start further child agents.
"""

from __future__ import annotations

from voidx.agent.domain.ui_events import GuidanceSubmitted, PermissionToolDetail, SubagentFinished, SubagentStarted
from voidx.agent.domain.display_policy import DEFAULT_DISPLAY_RULES, ToolDisplayPolicy

from voidx.agent.adapters.langgraph.runtime.core.helpers import _invalidate_tui, _render_inline_compaction_guide

from voidx.agent.adapters.langgraph.runtime.turn_runner import TurnRunner
from voidx.agent.application.compaction_service import CompactionService
from voidx.agent.domain.compaction import CompactionResult, PreflightCompactionResult
from voidx.agent.adapters.langgraph.graph_compaction import GraphCompactionAdapter
from voidx.agent.adapters.langgraph.runtime.tool_executor import ToolExecutorAdapter
from typing import Any, Protocol, TYPE_CHECKING
from voidx.tooling.domain.authorization import PermissionDecision
from voidx.tooling.domain.risk import RiskLevel
from voidx.agent.domain.task.intent import PersonaName
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from voidx.agent.application.runtime_context import ContextCompilerCache, InteractionMode, raw_semantic_messages
from voidx.agent.adapters.external_context import strip_external_tool_context
from voidx.agent.adapters.langgraph.state import AgentState
from voidx.agent.domain.task.state import TaskState, goal_type_from_join
from voidx.agent.adapters.langgraph.runtime.streaming import extract_text
from voidx.agent.adapters.langgraph.runtime.topology import latest_ai_message
from voidx.llm.usage import estimate_context_tokens
from voidx.agent.adapters.langgraph.runtime.core.helpers import _invalidate_tui, _render_inline_compaction_guide

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage

from voidx.agent.application.agents import AgentDef
from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import CompactionCoordinator
from voidx.agent.adapters.langgraph.runtime.convergence import generate_fallback_summary
from voidx.agent.adapters.langgraph.runtime.core.helpers import (
    _interaction_mode_for_persona,
    _invalidate_tui,
    _persona_for_child_workflow,
)
from voidx.agent.adapters.langgraph.runtime.runtime import current_parent_tool_call_id as _current_parent_tool_call_id
from voidx.agent.adapters.langgraph.runtime.runtime_guards import RuntimeGuardState
from voidx.agent.adapters.langgraph.runtime.session_runtime import SessionRuntime
from voidx.agent.ports.presentation import NullPresentationSnapshotPort, PresentationSnapshotPort
from voidx.agent.adapters.langgraph.runtime.llm_turn import LlmTurn
from voidx.agent.adapters.langgraph.runtime.permission_flow import PermissionFlow, _tool_call_key
from voidx.agent.adapters.langgraph.runtime.session_runtime import _sanitize_generated_title
from voidx.agent.adapters.langgraph.runtime.tool_executor import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent as _run_subagent
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    GuidanceEntry,
    clear_thread_execution_states,
    current_thread_execution_state,
)
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.adapters.langgraph.runtime.tool_executor import ToolExecutorAdapter
from voidx.agent.adapters.langgraph.runtime.topology import build_graph, session_date
from voidx.agent.adapters.langgraph.runtime.turn_metrics import TurnControlMetrics
from voidx.agent.adapters.langgraph.runtime.turn_runner import TurnRunner
from voidx.agent.adapters.langgraph.runtime.wiring import build_compaction_service
from voidx.agent.domain.task.state import GoalResolution, TaskState, goal_type_from_join
from voidx.tooling.application.ai_approval import AiApprovalService
from voidx.agent.application.instruction import InstructionService
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.structured import ainvoke_structured
from voidx.agent.adapters.persistence.session_repository import SessionInfo
from voidx.observability.tool_log import log_tool_event
from voidx.agent.adapters.persistence.subagent_repository import append_subagent_event
from voidx.agent.ports.ui import AgentUiPort
from voidx.agent.ports.workspace_lock import WorkspaceWriteLockPort

GUIDANCE_MAX_CHARS = 2_000


__all__ = [
    "LangGraphExecution",
    "SMART_TITLE_CHARS",
    "TEMPORARY_TITLE_CHARS",
    "TITLE_PERSONA_USER_CHARS",
    "TITLE_TIMEOUT_SECONDS",
    "_collapse_whitespace",
    "_message_row_title_text",
    "_sanitize_generated_title",
]


def _llm_turn_for(execution: Any) -> "LlmTurn":
    return execution._llm_turn


def _permission_flow_for(execution: Any) -> "PermissionFlow":
    return execution._permission_flow


def _session_runtime_for(execution: Any) -> SessionRuntime:
    return execution._session_runtime


def _turn_runner_for(execution: Any) -> TurnRunner:
    return execution._turn_runner


def _compaction_component_for(execution: Any) -> CompactionCoordinator:
    return execution._compaction_coordinator


def _tool_executor_for(execution: Any) -> ToolExecutorAdapter:
    return execution._tool_executor

















class PermissionConfig(Protocol):
    permission_mode: Any
    sandbox_readable_files: list[str]
    sandbox_readable_dirs: list[str]
    sandbox_writable_files: list[str]
    sandbox_writable_dirs: list[str]


class ExecutionConfig(PermissionConfig, Protocol):
    model: Any
    workspace: str
    user_profile: Any
    lsp_format_after_edit: bool
    compaction_soft_ratio: float
    compaction_post_target_ratio: float

    def model_copy(self, *, deep: bool = False) -> "ExecutionConfig": ...


class RuntimeConfigPort(Protocol):
    async def resolve_profile(self) -> Any: ...

    async def build_config(self, *, profile: Any) -> ExecutionConfig: ...



class LangGraphExecution:
    """LangGraph-backed agent execution infrastructure."""
    async def _call_llm(self, state: AgentState) -> dict:
        return await _llm_turn_for(self).call(state)

    async def _prepare_with_stream(self, state: AgentState) -> dict:
        return await _llm_turn_for(self).prepare_with_stream(state)

    async def _authorize_tool_calls(
        self: Any,
        tool_calls: list[dict],
        *,
        runtime_persona: str = PersonaName.COORDINATE,
        plan_mode: bool,
        session_id: str,
        interaction_mode: str | None = None,
        workflow_runs: object = (),
    ) -> tuple[list[dict], list[tuple[dict, str]]]:
        return await _permission_flow_for(self)._authorize_tool_calls(tool_calls, runtime_persona=runtime_persona, plan_mode=plan_mode, session_id=session_id, interaction_mode=interaction_mode, workflow_runs=workflow_runs)

    async def _ask_and_apply_permission(
        self: Any,
        need_ask: list[PermissionDecision],
        approved: list[dict],
        denied: list[tuple[dict, str]],
    ) -> None:
        return await _permission_flow_for(self)._ask_and_apply_permission(need_ask, approved, denied)

    async def _ask_tool_permission(
        self: Any,
        tool_calls: list[dict] | list[PermissionDecision],
        request_id: str | None = None,
    ) -> str | None:
        return await _permission_flow_for(self)._ask_tool_permission(
            tool_calls,
            request_id=request_id,
        )

    def _show_permission_output(self: Any, message: str) -> bool:
        return _permission_flow_for(self)._show_permission_output(message)

    def _notice_permission_result(self: Any, message: str) -> None:
        return _permission_flow_for(self)._notice_permission_result(message)

    def _permission_tool_details(self: Any, decisions: list[PermissionDecision]) -> list[PermissionToolDetail]:
        return _permission_flow_for(self)._permission_tool_details(decisions)


    @property
    def _session(self) -> SessionInfo | None:
        state = self._current_thread_state()
        return state.session if state is not None else getattr(self, "_default_session", None)

    @_session.setter
    def _session(self, value: SessionInfo | None) -> None:
        state = self._current_thread_state()
        if state is not None:
            state.session = value
        else:
            self._default_session = value

    @property
    def _session_msg_cache(self) -> list | None:
        state = current_thread_execution_state()
        return state.session_msg_cache if state is not None else getattr(self, "_default_session_msg_cache", None)

    @_session_msg_cache.setter
    def _session_msg_cache(self, value: list | None) -> None:
        state = current_thread_execution_state()
        if state is not None:
            state.session_msg_cache = value
        else:
            self._default_session_msg_cache = value

    @property
    def _context_cache(self) -> ContextCompilerCache:
        state = current_thread_execution_state()
        if state is not None:
            return state.context_cache
        if not hasattr(self, "_default_context_cache"):
            self._default_context_cache = ContextCompilerCache()
        return self._default_context_cache

    @_context_cache.setter
    def _context_cache(self, value: ContextCompilerCache) -> None:
        state = current_thread_execution_state()
        if state is not None:
            state.context_cache = value
        else:
            self._default_context_cache = value

    @property
    def _interaction_mode(self) -> InteractionMode:
        state = current_thread_execution_state()
        return state.interaction_mode if state is not None else getattr(self, "_default_interaction_mode", InteractionMode.AUTO)

    @_interaction_mode.setter
    def _interaction_mode(self, value: InteractionMode) -> None:
        state = current_thread_execution_state()
        if state is not None:
            state.interaction_mode = value
        else:
            self._default_interaction_mode = value

    def _current_thread_state(self):
        state = current_thread_execution_state()
        if state is None or getattr(state, "host_id", None) != id(self):
            return None
        return state

    @property
    def _task_state(self) -> TaskState:
        state = self._current_thread_state()
        if state is not None:
            return state.task_state
        if not hasattr(self, "_default_task_state"):
            self._default_task_state = TaskState()
        return self._default_task_state

    @_task_state.setter
    def _task_state(self, value: TaskState) -> None:
        state = self._current_thread_state()
        if state is not None:
            state.task_state = value
            default_session = getattr(self, "_default_session", None)
            if (
                state.session is None
                or default_session is None
                or state.session.id == default_session.id
            ):
                self._default_task_state = value
        else:
            self._default_task_state = value

    @property
    def _compaction_summary(self) -> str:
        state = current_thread_execution_state()
        return state.compaction_summary if state is not None else getattr(self, "_default_compaction_summary", "")

    @_compaction_summary.setter
    def _compaction_summary(self, value: str) -> None:
        state = current_thread_execution_state()
        if state is not None:
            state.compaction_summary = value
        else:
            self._default_compaction_summary = value

    @property
    def _pending_summary(self) -> str | None:
        state = current_thread_execution_state()
        return state.pending_summary if state is not None else getattr(self, "_default_pending_summary", None)

    @_pending_summary.setter
    def _pending_summary(self, value: str | None) -> None:
        state = current_thread_execution_state()
        if state is not None:
            state.pending_summary = value
        else:
            self._default_pending_summary = value

    @property
    def _session_date(self) -> str:
        state = current_thread_execution_state()
        return state.session_date if state is not None else getattr(self, "_default_session_date", "")

    @_session_date.setter
    def _session_date(self, value: str) -> None:
        state = current_thread_execution_state()
        if state is not None:
            state.session_date = value
        else:
            self._default_session_date = value

    @property
    def _runtime_guards(self) -> RuntimeGuardState:
        state = current_thread_execution_state()
        if state is not None:
            return state.runtime_guards
        if not hasattr(self, "_default_runtime_guards"):
            self._default_runtime_guards = RuntimeGuardState()
        return self._default_runtime_guards

    @_runtime_guards.setter
    def _runtime_guards(self, value: RuntimeGuardState) -> None:
        state = current_thread_execution_state()
        if state is not None:
            state.runtime_guards = value
        else:
            self._default_runtime_guards = value

    def __init__(
        self,
        config: ExecutionConfig,
        api_key: str | None,
        session: SessionInfo | None = None,
        settings: RuntimeConfigPort | None = None,
        model_catalog: Any | None = None,
        model_catalog_factory: Callable[[Any | None], Any] | None = None,
        skills_api: Any | None = None,
        skills_api_factory: Callable[[Any | None], Any] | None = None,
        skills_api_provider: Callable[[str], Any] | None = None,
        *,
        ui: AgentUiPort,
        workspace_write_lock: WorkspaceWriteLockPort,
        presentation_snapshots: PresentationSnapshotPort | None = None,
        external_manager_factory: Callable[..., tuple[Any, Any]] | None = None,
        mcp_reference_resolver: Callable[..., Awaitable[Any]] | None = None,
        web_route: Callable[..., Awaitable[Any]] | None = None,
        permission_service_factory: Callable[..., Any],
        model_factory: Callable[..., Any],
        resolver_model_factory: Callable[..., Any],
        tool_registry_factory: Callable[..., Any],
        scoped_tools_binder: Callable[..., None],
        profile_tool_registry_factory: Callable[..., Any],
        slash_handler_factory: Callable[[Any], Any],
        reasoning_effort_type: Any,
        context_limit_resolver: Callable[..., int],
        provider_specs: Any,
        language_labels: Any,
        tone_labels: Any,
        update_service: Any | None = None,
        clipboard_image: Any | None = None,
        available_servers_renderer: Callable[..., str] | None = None,
    ):
        self.config = config
        self.api_key = api_key
        self.model = model_factory(api_key, config.model) if api_key else None
        self._session = session
        self.agent_gateway = InProcessSubagentGateway()
        self._workspace = config.workspace
        self._settings = settings
        self._model_catalog_factory = model_catalog_factory
        if model_catalog is None:
            from voidx.llm.application.model_catalog import ModelCatalog
            from voidx.llm.providers.catalog import PROVIDER_SPECS

            model_catalog = ModelCatalog(
                provider_specs=PROVIDER_SPECS,
                settings=settings,
            )
        self.model_catalog = model_catalog
        self._skills_api_factory = skills_api_factory
        self.skills_api_provider = skills_api_provider
        if skills_api is None:
            if self.skills_api_provider is None:
                raise RuntimeError("skills_api is required")
            skills_api = self.skills_api_provider(self._workspace)
        self.skills_api = skills_api
        self._mcp_reference_resolver = mcp_reference_resolver
        self._web_route = web_route
        self._permission_service_factory = permission_service_factory
        self._model_factory = model_factory
        self._resolver_model_factory = resolver_model_factory
        self._available_servers_renderer = available_servers_renderer
        self._historical_tool_context_stripper = strip_external_tool_context
        self._tool_registry_factory = tool_registry_factory
        self._scoped_tools_binder = scoped_tools_binder
        self._profile_tool_registry_factory = profile_tool_registry_factory
        self.reasoning_effort_type = reasoning_effort_type
        self.context_limit_resolver = context_limit_resolver
        self.provider_specs = provider_specs
        self.language_labels = language_labels
        self.tone_labels = tone_labels
        if update_service is None:
            raise RuntimeError("update_service is required")
        self.update_service = update_service
        if clipboard_image is None:
            raise RuntimeError("clipboard_image is required")
        self.clipboard_image = clipboard_image
        self._ai_approval = AiApprovalService(
            model_factory=model_factory,
            resolver_model_factory=resolver_model_factory,
            structured_invoker=ainvoke_structured,
        )
        self._ui = ui
        self._workspace_write_lock = workspace_write_lock
        self._any_messages_sent = False
        self._startup_presenter = None
        self._coding_turn_runner: Callable[..., Awaitable[Any]] | None = None
        self.loop_service = None
        self.goal_service = None

        self._tracker, self.tools = self._tool_registry_factory(
            settings=settings,
            config=config,
            subagent_runner=self._subagent_runner,
            skills_api_provider=self.skills_api_provider,
            web_route=self._web_route,
        )

        self._instruction = InstructionService(
            self._workspace,
            settings=settings,
            skill_summaries_provider=self._available_skill_summaries,
            **(
                {"available_servers_renderer": self._available_servers_renderer}
                if self._available_servers_renderer is not None
                else {}
            ),
        )
        self._permission = self._permission_service_factory(config, settings=self._settings, notifier=self._ui.ui.print)

        self._interaction_mode: InteractionMode = InteractionMode.AUTO
        self._debug: bool = False
        self._instruction.set_debug(self._debug)
        self._ui.ui.set_debug(self._debug)

        self._file_mtimes: dict[str, dict[str, int]] = {}
        self._file_read_coverage: dict[str, dict] = {}
        self._workflow_repeat_tracker: dict[str, dict[str, int]] = {}
        self._turn_node: Any | None = None
        self._current_tree: Any | None = None
        self._current_messages: list[BaseMessage] | None = None
        self._pending_summary: str | None = None
        self._compaction_summary: str = ""
        self._session_date: str = session_date(session)
        self._session_msg_cache: list | None = None
        self._context_cache = ContextCompilerCache()
        self._next_agent_id: int = 0
        self._task_state = TaskState()
        self._needs_failure_check: dict[str, dict] = {}
        self._successful_dangerous_calls: set[str] = set()
        self._successful_dangerous_calls_session_id: str | None = None
        self._runtime_guards = RuntimeGuardState()
        self._turn_metrics = TurnControlMetrics()
        self._pending_turn_stop_commit: dict[str, Any] | None = None
        self._pending_guidance: list[GuidanceEntry] = []
        self._guidance_service: Any | None = None
        self._clear_session_tasks: set[asyncio.Task[None]] = set()
        self._title_generation: int = 0
        self._title_task: asyncio.Task[None] | None = None
        self._usage_stats, self._compaction = build_compaction_service(config)
        self._compaction_coordinator = CompactionCoordinator(self)
        self._llm_turn = LlmTurn(self)
        self._permission_flow = PermissionFlow(self)
        self._session_runtime = SessionRuntime(
            self,
            presentation_snapshots=presentation_snapshots or NullPresentationSnapshotPort(),
        )
        self._tool_executor = ToolExecutorAdapter(self)
        self._turn_runner = TurnRunner(self)

        display_config = getattr(config, "display_policy", None) or {}
        self._display_policy = ToolDisplayPolicy.from_config(display_config, defaults=DEFAULT_DISPLAY_RULES)

        self._build()
        self._slash = slash_handler_factory(self)
        self._mcp_manager = None
        self._lsp_manager = None
        if external_manager_factory is not None:
            self._mcp_manager, self._lsp_manager = external_manager_factory(
                settings=self._settings,
                tools=self.tools,
                permission=self._permission,
                workspace=self._workspace,
                model=self.model,
                model_config=self.config.model,
            )
            self._instruction.set_mcp_description_provider(self._mcp_manager.generated_descriptions)
        self._lsp_operations = None
        if self._lsp_manager is not None:
            from voidx.lsp.application.service import LspOperationsService

            self._lsp_operations = LspOperationsService(self._lsp_manager)
        if TYPE_CHECKING:
            _host_contract: Any = self

    @property
    def ui(self):
        return self._ui.ui

    @property
    def presentation_ui(self):
        return self._ui


    @property
    def compaction(self):
        return self._compaction

    def model_factory(self, api_key, config):
        return self._model_factory(api_key, config)

    def interaction_mode_value(self) -> str:
        return self._interaction_mode.value

    def invalidate_skill_service_cache(self) -> None:
        self._invalidate_skill_service_cache()

    @property
    def runtime_guards(self):
        return self._runtime_guards

    async def apply_settings_update(self, settings: RuntimeConfigPort) -> None:
        await self._apply_settings_update(settings)

    @property
    def plan_mode(self) -> bool:
        return self._plan_mode

    @property
    def slash(self):
        return self._slash


    @property
    def any_messages_sent(self) -> bool:
        return self._any_messages_sent

    @any_messages_sent.setter
    def any_messages_sent(self, value: bool) -> None:
        self._any_messages_sent = value

    @property
    def compaction_summary(self) -> str:
        return self._compaction_summary

    def set_compaction_summary(self, value: str) -> None:
        self._compaction_summary = value

    @property
    def session_date(self) -> str:
        return self._session_date

    def set_session_date(self, value: str) -> None:
        self._session_date = value


    @property
    def permission(self):
        return self._permission

    @property
    def session(self) -> SessionInfo | None:
        return self._session

    @property
    def settings(self) -> RuntimeConfigPort | None:
        return self._settings

    @property
    def task_state(self) -> TaskState:
        return self._task_state

    def _skills_api_for_current_workspace(self):
        state = current_thread_execution_state()
        workspace = state.workspace if state is not None and state.workspace else self._workspace
        if workspace == self._workspace or self.skills_api_provider is None:
            return self.skills_api
        return self.skills_api_provider(workspace)

    def _resolve_skill_references(self, user_text: str):
        return self._skills_api_for_current_workspace().resolve_references(user_text)

    def _available_skill_summaries(self):
        return self._skills_api_for_current_workspace().service.available_skill_summaries()

    def _skill_service_for_references(self):
        return self._skills_api_for_current_workspace().service

    def _invalidate_skill_service_cache(self) -> None:
        if self._skills_api_factory is not None:
            self.skills_api = self._skills_api_factory(self._settings)
        self._ui.invalidate_skill_service_cache()

    @property
    def usage_stats(self):
        return self._usage_stats

    async def _apply_settings_update(self, settings: RuntimeConfigPort) -> None:
        profile = await settings.resolve_profile()
        new_config = await settings.build_config(profile=profile)
        new_config.workspace = self._workspace

        self.clear_successful_dangerous_calls()
        self._settings = settings
        self.config = new_config
        self.api_key = profile.api_key if profile is not None else None
        self.model = self._model_factory(self.api_key, self.config.model) if self.api_key else None
        if self._mcp_manager is not None:
            self._mcp_manager.set_description_model(self.model)

        if self._model_catalog_factory is not None:
            self.model_catalog = self._model_catalog_factory(settings)
        if self._skills_api_factory is not None:
            self.skills_api = self._skills_api_factory(settings)
        self._tracker, self.tools = self._tool_registry_factory(
            settings=settings,
            config=self.config,
            subagent_runner=self._subagent_runner,
            skills_api_provider=self.skills_api_provider,
            web_route=self._web_route,
        )
        old_permission = getattr(self, "_permission", None)
        self._permission = self._permission_service_factory(self.config, settings=settings, notifier=self._ui.ui.print)
        if old_permission is not None and hasattr(old_permission, "ai_approval_count"):
            self._permission.ai_approval_count = old_permission.ai_approval_count
        self._tool_executor = ToolExecutorAdapter(self)

        context_limit = self._compaction.context_limit
        updated_usage_stats, updated_compaction = build_compaction_service(self.config)
        context_limit = updated_usage_stats.context_limit or context_limit
        self._usage_stats.context_limit = context_limit
        self._compaction.context_limit = updated_compaction.context_limit
        self._compaction.output_token_max = updated_compaction.output_token_max
        self._compaction.soft_ratio = updated_compaction.soft_ratio
        self._compaction.post_target_ratio = updated_compaction.post_target_ratio

        self._ui.update_status(
            provider=self.config.model.provider,
            model=self.config.model.model,
            context_limit=context_limit,
            reasoning_effort=(
                self.config.model.reasoning_effort.value
                if self.config.model.reasoning_effort is not None
                else "xhigh"
            ),
        )

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

    @property
    def debug_enabled(self) -> bool:
        return self._debug


    def set_task_state(self, task_state: TaskState) -> None:
        self._task_state = task_state

    def can_submit_guidance(self) -> bool:
        return True

    def submit_guidance(
        self,
        text: str,
        *,
        source: Literal["user", "guard"] = "user",
        thread_id: str = "",
        session_id: str = "",
    ) -> bool:
        guidance = " ".join(text.strip().split())
        if not guidance:
            return False
        guidance_service = getattr(self, "_guidance_service", None)
        if guidance_service is not None:
            current_state = self._current_thread_state()
            current_session = current_state.session if current_state is not None else self._session
            submitted = guidance_service.submit_guidance(
                guidance,
                source=source,
                thread_id=thread_id or (
                    current_state.thread_id if current_state is not None else ""
                ),
                session_id=session_id or (
                    current_session.id if current_session is not None else ""
                ),
            )
            return submitted is not None

        truncated = False
        if len(guidance) > GUIDANCE_MAX_CHARS:
            guidance = guidance[:GUIDANCE_MAX_CHARS].rstrip()
            truncated = True
        if source == "user" and self._ui.via_events():
            if not self._ui.events.emit_direct(
                GuidanceSubmitted(text=guidance, truncated=truncated)
            ):
                return False
        current_state = self._current_thread_state()
        current_session = current_state.session if current_state is not None else self._session
        entry = GuidanceEntry(
            text=guidance,
            truncated=truncated,
            source=source,
            thread_id=thread_id or (current_state.thread_id if current_state is not None else ""),
            session_id=session_id or (current_session.id if current_session is not None else ""),
        )
        target_state = self._guidance_target_state(entry)
        if target_state is not None:
            target_state.pending_guidance.append(entry)
        else:
            self._pending_guidance.append(entry)
        return True

    def _guidance_target_state(self, entry: GuidanceEntry):
        current_state = self._current_thread_state()
        if current_state is not None and self._guidance_matches_state(entry, current_state):
            return current_state
        if not entry.thread_id:
            return None
        states = getattr(self, "_thread_execution_states", {})
        for state in states.values():
            if self._guidance_matches_state(entry, state):
                return state
        return None

    def _guidance_matches_state(self, entry: GuidanceEntry, state) -> bool:
        if getattr(state, "host_id", None) not in (None, id(self)):
            return False
        if entry.thread_id and getattr(state, "thread_id", "") != entry.thread_id:
            return False
        state_session = getattr(state, "session", None)
        state_session_id = state_session.id if state_session is not None else ""
        if entry.session_id and state_session_id != entry.session_id:
            return False
        return bool(entry.thread_id or entry.session_id)

    def _guidance_matches_current_thread(self, entry: GuidanceEntry) -> bool:
        current_state = self._current_thread_state()
        current_thread_id = current_state.thread_id if current_state is not None else ""
        current_session = current_state.session if current_state is not None else self._session
        current_session_id = current_session.id if current_session is not None else ""
        if entry.thread_id and current_thread_id != entry.thread_id:
            return False
        if entry.session_id and current_session_id != entry.session_id:
            return False
        return True

    def _pop_pending_guidance(self) -> list[GuidanceEntry]:
        entries: list[GuidanceEntry] = []
        current_state = self._current_thread_state()
        if current_state is not None:
            remaining_state_entries: list[GuidanceEntry] = []
            for entry in current_state.pending_guidance:
                if self._guidance_matches_current_thread(entry):
                    entries.append(entry)
                else:
                    remaining_state_entries.append(entry)
            current_state.pending_guidance = remaining_state_entries

        remaining_entries: list[GuidanceEntry] = []
        for entry in self._pending_guidance:
            if self._guidance_matches_current_thread(entry):
                entries.append(entry)
            else:
                remaining_entries.append(entry)
        self._pending_guidance = remaining_entries
        return entries

    def _discard_pending_guidance(self) -> bool:
        return bool(self._pop_pending_guidance())

    def _drain_pending_guidance(self) -> list[tuple[HumanMessage, bool, Literal["user", "guard"]]]:
        messages: list[tuple[HumanMessage, bool, Literal["user", "guard"]]] = []
        for entry in self._pop_pending_guidance():
            messages.append((
                HumanMessage(
                    content=entry.text,
                    additional_kwargs={GUIDANCE_MARKER: True},
                ),
                entry.truncated,
                entry.source,
            ))
        return messages

    async def persist_runtime_state(self) -> None:
        await self._persist_runtime_state()

    async def compact_session_history(self, *, force: bool = True) -> bool:
        return await self._compact_session_history(force=force)

    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        return await self._restore_transcript_snapshot(append=append)

    def bind_startup_presenter(self, presenter) -> None:
        self._startup_presenter = presenter

    def bind_presentation_snapshots(self, snapshots: PresentationSnapshotPort) -> None:
        self._session_runtime.presentation_snapshots = snapshots

    def bind_guidance_service(self, guidance_service: Any) -> None:
        self._guidance_service = guidance_service
        add_callback = getattr(guidance_service, "add_submitted_callback", None)
        if callable(add_callback):
            add_callback(self._project_submitted_guidance)

    def bind_automation_services(self, loop_service, goal_service) -> None:
        self.loop_service = loop_service
        self.goal_service = goal_service

    def _project_submitted_guidance(self, guidance: Any) -> None:
        source = guidance.source if guidance.source in {"user", "guard"} else "guard"
        if source == "user" and self._ui.via_events():
            self._ui.events.emit_direct(
                GuidanceSubmitted(text=guidance.text, truncated=guidance.truncated)
            )
        entry = GuidanceEntry(
            text=guidance.text,
            truncated=guidance.truncated,
            source=source,
            thread_id=guidance.target_thread_id or "",
            session_id=guidance.target_session_id or "",
            guidance_id=guidance.guidance_id,
        )
        target_state = self._guidance_target_state(entry)
        if target_state is not None:
            target_state.pending_guidance.append(entry)
        else:
            self._pending_guidance.append(entry)

    def bind_coding_turn_runner(
        self,
        runner: Callable[..., Awaitable[Any]],
    ) -> None:
        self._coding_turn_runner = runner

    async def run_coding_turn(
        self,
        text: str,
        *,
        display_text: str | None = None,
    ) -> None:
        if self._coding_turn_runner is None:
            raise RuntimeError("coding turn runner is not bound")
        await self._coding_turn_runner(text, display_text=display_text)

    async def show_startup(
        self,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> None:
        if self._startup_presenter is None:
            raise RuntimeError("startup presenter is not bound")
        await self._startup_presenter(
            append_transcript=append_transcript,
            prefer_direct=prefer_direct,
        )


    async def clear_current_session(self) -> None:
        self._invalidate_session_title_generation()
        old_session_id = self._session.id if self._session is not None else None
        if old_session_id:
            await self.agent_gateway.close_session(old_session_id)
            clear_thread_execution_states(self, old_session_id)
        self._session = None
        self._session_date = session_date(None)
        self._session_msg_cache = []
        self._context_cache = ContextCompilerCache()
        self._reset_runtime_state_memory()
        self._tracker.clear_todos()
        self._permission.clear_session_permissions()
        self._usage_stats.reset()
        self._current_messages = None
        self._pending_guidance.clear()
        if old_session_id:
            await self._clear_session_storage(old_session_id)

    def _schedule_clear_session_storage(self, session_id: str) -> None:
        task = asyncio.create_task(
            self._clear_session_storage(session_id),
            name=f"voidx-clear-session-{session_id}",
        )
        self._clear_session_tasks.add(task)
        task.add_done_callback(self._clear_session_tasks.discard)

    async def _clear_session_storage(self, session_id: str) -> None:
        from voidx.agent.adapters.persistence.session_repository import clear_messages, update_title

        try:
            await clear_messages(session_id)
            await self._session_runtime.presentation_snapshots.clear(session_id)
            await update_title(session_id, "New session", touch=False)
        except Exception as exc:
            self._ui.ui.print(f"[red]Clear cleanup failed: {exc}[/red]")


    async def resume_session(self, session: SessionInfo) -> None:
        self._invalidate_session_title_generation()
        old_session_id = self._session.id if self._session is not None else None
        if old_session_id and old_session_id != session.id:
            await self.agent_gateway.close_session(old_session_id)
        self._session = session
        self._workspace = session.workspace
        self.config.workspace = session.workspace
        self._session_date = session_date(session)
        self._session_msg_cache = None
        self._context_cache = ContextCompilerCache()
        await self.restore_runtime_state()
        try:
            from voidx.agent.adapters.persistence.context_frame_repository import gc_context_frames

            await gc_context_frames(session.id)
        except Exception as exc:
            log_tool_event("context_frame_gc_failed", message=str(exc), session_id=session.id)
        await self._resume_loop_for_session(session)

    async def _resume_loop_for_session(self, session: SessionInfo) -> None:
        """A resumed session takes back ownership of its loop's wakeups."""
        loop_service = self.loop_service
        if loop_service is None:
            return
        try:
            await loop_service.resume(session.id)
        except Exception:
            import logging

            logging.getLogger(__name__).warning("loop resume failed for session %s", session.id)

    async def set_session_title(self, title: str) -> None:
        if self._session is None:
            return

        from voidx.agent.adapters.persistence.session_repository import update_title

        self._invalidate_session_title_generation()
        await update_title(self._session.id, title)
        self._session = self._session.model_copy(update={"title": title})

    async def _subagent_runner(
        self,
        agent_def: AgentDef,
        description: str,
        goal_resolution: GoalResolution,
        result_contract: Any,
        *,
        permission_snapshot=None,
        agent_run_id: str | None = None,
        agent_gateway=None,
        run_metadata: dict[str, object] | None = None,
    ) -> str:
        run_metadata = run_metadata if run_metadata is not None else {}
        sub_buffer: list[BaseMessage] = []
        session_id = self._session.id if self._session else "default"
        agent_id = self._next_agent_id
        self._next_agent_id += 1
        parent_tool_call_id = _current_parent_tool_call_id.get()
        agent_run_id = agent_run_id or f"agent_{agent_id}"
        agent_gateway = agent_gateway or getattr(self, "agent_gateway", None)
        started_at = time.monotonic()
        goal = goal_resolution.goal
        plan = goal_resolution.plan
        workflow_start = plan.join if plan is not None else ""
        goal_type = goal_type_from_join(workflow_start)
        try:
            thread_state = current_thread_execution_state()
            turn_context = thread_state.turn_context if thread_state is not None else None
            workflow_profile_context = turn_context.workflow_context if turn_context is not None else None
            workflow_dag = workflow_profile_context.dag if workflow_profile_context is not None else None
            workflow_runtime_context = await self._workflow_context_for(
                goal_type=goal_type,
                scope=goal.label if goal is not None else description,
                workflow_start=workflow_start,
                workflow_dag=workflow_dag,
            )
            runtime_persona = _persona_for_child_workflow(
                workflow_runtime_context.runs,
                workflow_start,
                workflow_dag,
            )
            interaction_mode = _interaction_mode_for_persona(runtime_persona)
        except Exception as exc:
            error = str(exc).strip()[:500] or exc.__class__.__name__
            if self._ui.via_events():
                await self._ui.events.emit(SubagentFinished(
                    agent_id=agent_id,
                    subagent_id=agent_run_id,
                    ok=False,
                    elapsed=time.monotonic() - started_at,
                    finish_reason="error",
                    error=error,
                ))
            if self._session:
                await append_subagent_event(session_id, agent_run_id, {
                    "type": "subagent_finish",
                    "agent_id": agent_id,
                    "ok": False,
                    "elapsed": time.monotonic() - started_at,
                    "finish_reason": "error",
                    "error": error,
                })
            raise

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
                name=agent_def.name,
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
            })

        ok = False
        result = ""
        error = ""
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
                "run_metadata": run_metadata,
                "permission_snapshot": permission_snapshot,
                "agent_run_id": agent_run_id,
                "agent_gateway": agent_gateway,
                "model_factory": self._model_factory,
                "scoped_tools_binder": self._scoped_tools_binder,
            }
            if self._current_tree and self._turn_node:
                kwargs.update({
                    "capture_tree": self._current_tree,
                    "parent_node": self._turn_node,
                })
            result = await _run_subagent(
                agent_def,
                description,
                self.api_key,
                self.config,
                self._tracker,
                runtime_persona=runtime_persona,
                goal_resolution=goal_resolution,
                result_contract=result_contract,
                ui_port=self._ui,
                **kwargs,
            )
            ok = True
            return result
        except Exception as exc:
            error = str(exc).strip()[:500] or exc.__class__.__name__
            raise
        finally:
            if self._ui.via_events():
                await self._ui.events.emit(SubagentFinished(
                    agent_id=agent_id,
                    subagent_id=agent_run_id,
                    ok=ok,
                    elapsed=time.monotonic() - started_at,
                    finish_reason=str(run_metadata.get("finish_reason") or ("final_answer" if ok else "error")),
                    summary=result if ok else "",
                    error=error,
                    calls=int(run_metadata.get("calls") or 0),
                    tokens=int(run_metadata.get("tokens") or 0),
                ))
            if self._session:
                finish_event = {
                    "type": "subagent_finish",
                    "agent_id": agent_id,
                    "ok": ok,
                    "elapsed": time.monotonic() - started_at,
                    "finish_reason": str(run_metadata.get("finish_reason") or ("final_answer" if ok else "error")),
                }
                if not ok and error:
                    finish_event["error"] = error
                await append_subagent_event(session_id, agent_run_id, finish_event)

    def set_debug(self, value: bool) -> None:
        self._debug = value
        self._instruction.set_debug(value)
        self._ui.ui.set_debug(value)

    def _build(self) -> None:
        self.graph = build_graph(self)

    def _invalidate_session_title_generation(self: "Any") -> None:
        _session_runtime_for(self).invalidate_session_title_generation()

    def _temporary_session_title(self: "Any", text: str) -> str:
        return _session_runtime_for(self).temporary_session_title(text)

    def _schedule_session_title_generation(
        self: "Any",
        session_id: str,
        first_user_text: str,
        temporary_title: str,
    ) -> None:
        _session_runtime_for(self).schedule_session_title_generation(
            session_id,
            first_user_text,
            temporary_title,
            invalidate_session_title_generation=self._invalidate_session_title_generation,
            generate_session_title=self._generate_session_title,
            finish_title_task=self._finish_title_task,
        )

    def _finish_title_task(self: "Any", task: asyncio.Task[None]) -> None:
        _session_runtime_for(self).finish_title_task(task)

    async def _generate_session_title(
        self: "Any",
        session_id: str,
        generation_id: int,
        first_user_text: str,
        temporary_title: str,
    ) -> None:
        await _session_runtime_for(self).generate_session_title(
            session_id,
            generation_id,
            first_user_text,
            temporary_title,
            run_title_agent=self._run_title_agent,
            can_apply_generated_title=self._can_apply_generated_title,
        )

    async def _run_title_agent(self: "Any", first_user_text: str) -> str | None:
        return await _session_runtime_for(self).run_title_agent(first_user_text)

    def _can_apply_generated_title(
        self: "Any",
        session_id: str,
        generation_id: int,
        temporary_title: str,
    ) -> bool:
        return _session_runtime_for(self).can_apply_generated_title(
            session_id,
            generation_id,
            temporary_title,
        )

    async def regenerate_session_title(self: "Any") -> bool:
        return await _session_runtime_for(self).regenerate_session_title(
            temporary_session_title=self._temporary_session_title,
            schedule_session_title_generation=self._schedule_session_title_generation,
        )

    async def delete_empty_current_session(self) -> None:
        await _session_runtime_for(self).delete_empty_current_session(
            invalidate_session_title_generation=self._invalidate_session_title_generation,
        )

    def _session_component(self: Any) -> SessionRuntime:
        return _session_runtime_for(self)

    def _reset_runtime_state_memory(self: Any) -> None:
        _session_runtime_for(self).reset_runtime_state_memory()

    async def restore_runtime_state(self) -> None:
        await _session_runtime_for(self).restore_runtime_state()

    async def _persist_runtime_state(self: Any) -> None:
        await _session_runtime_for(self).persist_runtime_state()

    async def _clear_runtime_state(self: Any) -> None:
        await _session_runtime_for(self).clear_runtime_state(
            reset_runtime_state_memory=self._reset_runtime_state_memory,
        )

    async def _persist_transcript_snapshot(self: Any) -> None:
        await _session_runtime_for(self).persist_transcript_snapshot()

    async def _restore_transcript_snapshot(self: Any, *, append: bool = False) -> bool:
        return await _session_runtime_for(self).restore_transcript_snapshot(append=append)

    def _turn_runner_component(self: Any) -> TurnRunner:
        return _turn_runner_for(self)

    def runtime_snapshot(self):
        from voidx.agent.domain.turn.state import TurnPhase
        from voidx.agent.adapters.langgraph.state_mapper import LangGraphStateMapper

        return LangGraphStateMapper().runtime_from_execution(self, turn_phase=TurnPhase.RUNNING)

    @property
    def session_id(self) -> str:
        return self._session.id if self._session is not None else ""

    async def run_turn(
        self,
        user_text: str,
        *,
        display_text: str | None = None,
        context: TurnExecutionContext,
        persist_user_input: bool = True,
        guidance: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        if not isinstance(context, TurnExecutionContext):
            raise TypeError("run_turn requires TurnExecutionContext")
        await _turn_runner_for(self).run_once(
            user_text,
            display_text=display_text,
            context=context,
            persist_user_input=persist_user_input,
            guidance=guidance,
        )


    def _compaction_component(self: Any) -> CompactionCoordinator:
        return _compaction_component_for(self)

    async def _maybe_compact(
        self: Any,
        messages: list,
        session_msgs: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
        preflight: bool = False,
    ) -> tuple[list | None, str | None]:
        coordinator = _compaction_component_for(self)
        custom_compaction_agent = self.__dict__.get("_run_compaction_agent")
        service = CompactionService(
            GraphCompactionAdapter(
                coordinator,
                run_compaction_agent=custom_compaction_agent,
                persist_compaction=self._persist_compaction,
            )
        )
        return await service.compact_live_messages(
            messages,
            session_msgs,
            force=force,
            ask=ask,
            preflight=preflight,
        )

    async def _preflight_compact_if_needed(
        self: Any,
        messages: list,
        session_msgs: list | None = None,
        *,
        force: bool = False,
        reason: str = "threshold",
        ask: bool = False,
    ) -> tuple[CompactionResult | None, PreflightCompactionResult]:
        result, preflight_result = await _compaction_component_for(self).preflight_compact_if_needed(
            messages,
            session_msgs,
            force=force,
            reason=reason,
            ask=ask,
            run_compaction_agent=self.__dict__.get("_run_compaction_agent"),
            persist_compaction=self._persist_compaction,
        )
        if result is not None:
            self._file_read_coverage.clear()
            self._file_mtimes.clear()
        return result, preflight_result

    async def _ask_compact(self: Any, total_tokens: int) -> bool:
        return await _compaction_component_for(self).ask_compact(total_tokens)

    async def _persist_compaction(self: Any, head_messages: list) -> None:
        await _compaction_component_for(self).persist_compaction(head_messages)

    async def _compact_session_history(self: Any, *, force: bool = True) -> bool:
        result = await _compaction_component_for(self).compact_session_history(
            force=force,
            run_compaction_agent=self.__dict__.get("_run_compaction_agent"),
            persist_compaction=self._persist_compaction,
        )
        if result:
            self._file_read_coverage.clear()
            self._file_mtimes.clear()
        return result

    async def _run_compaction_agent(
        self: Any,
        head_messages: list,
        previous_summary: str | None,
    ) -> str | None:
        return await _compaction_component_for(self).run_compaction_agent(
            head_messages,
            previous_summary,
        )

    def _tool_execution_component(self: Any) -> ToolExecutorAdapter:
        return _tool_executor_for(self)

    async def _execute_tools(self: Any, state) -> dict:
        return await _tool_executor_for(self).execute_tools(
            state,
            tool_result_ok=self._tool_result_ok,
        )

    @staticmethod
    def _tool_result_ok(result) -> bool:
        return ToolExecutorAdapter.tool_result_ok(result)






    def _notify_tool_failure(self: Any, tc: dict, result) -> None:
        """Notify user when an auto-approved tool fails."""
        tool_name = tc.get("name", "unknown")
        error_preview = str(result.output)[:200]
        message = f"[on-failure] '{tool_name}' failed: {error_preview}"
        if not self._show_permission_output(message):
            self._ui.ui.print(f"\n[yellow]{message}[/yellow]")

    def _record_successful_tool_call(self: Any, tool_call: dict[str, Any]) -> None:
        risk = (tool_call.get("metadata") or {}).get("approved_risk") or {}
        if risk.get("risk_level") != RiskLevel.DANGEROUS.value:
            return
        key = _tool_call_key(tool_call)
        if key is not None:
            self._successful_dangerous_calls.add(key)

    def clear_successful_dangerous_calls(self: Any) -> None:
        self._successful_dangerous_calls.clear()
        self._successful_dangerous_calls_session_id = None

    def _clear_failure_check(self: Any, cid: str) -> None:
        """Remove a tool call ID from on-failure tracking (used on success)."""
        self._needs_failure_check.pop(cid, None)



    async def _workflow_context_for(self, *args, **kwargs):
        return await self._instruction.workflow_context_for(*args, **kwargs)

    def _invalidate_tui_for_turn(self) -> None:
        _invalidate_tui(self)

    def _inline_compaction_guide_for(self, messages: list[BaseMessage]) -> HumanMessage | None:
        if not getattr(self.config, "inline_compaction_enabled", False):
            return None
        total_tokens = estimate_context_tokens(messages, self.config.model.model)
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}
        if self._compaction.is_overflow(tokens):
            return None
        if total_tokens < self._compaction.usable_window():
            return None

        semantic_messages = raw_semantic_messages(messages)
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


    def _router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "execute"
        return "end"

    async def _finalize(self, state: AgentState) -> dict:
        from voidx.agent.adapters.langgraph.runtime.convergence import generate_fallback_summary

        messages: list = []
        if state.get("convergence_forced"):
            last = latest_ai_message(state.get("messages", []))
            if not (isinstance(last, AIMessage) and not last.tool_calls and len(extract_text(last).strip()) >= 20):
                messages.append(AIMessage(content=generate_fallback_summary(state)))
        running_notice = self._running_child_runs_notice()
        if running_notice is not None:
            messages.append(running_notice)
        return {"messages": messages} if messages else {}

    def _running_child_runs_notice(self):
        session = self._session
        if session is None:
            return None
        running = [
            run
            for run in self.agent_gateway.list_runs(session_id=session.id)
            if run.agent_type == "sub" and run.status in ("pending", "running")
        ]
        if not running:
            return None
        lines = "\n".join(f"- {run.run_id} ({run.agent_name}): {run.description}" for run in running)
        return HumanMessage(
            content=(
                f"{len(running)} background child agent run(s) still running:\n{lines}\n"
                "Use agent(wait) with target_run_id to collect results, or agent(cancel) to stop them."
            ),
            additional_kwargs={GUIDANCE_MARKER: True},
        )
