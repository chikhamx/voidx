"""Agent graph — LangGraph state machine.

voidx uses one primary agent identity (`voidx`) and runtime thinking-mode
personas (`coordinate`, `explore`, `plan`, `implement`, `review`).

Depth limit = 1: child agents cannot start further child agents.
"""

from __future__ import annotations

from voidx.agent.infrastructure.langgraph.runtime.core.context import (
    rebuild_llm_messages as build_llm_context_messages,
    replacement_messages as compacted_replacement_messages,
    rerender_task_context,
    save_main_context_frame,
)
from voidx.agent.infrastructure.langgraph.runtime.core.loop import LlmLoopState, handle_llm_exception
from voidx.agent.infrastructure.langgraph.runtime.core.turn import handle_turn_control_response
from voidx.agent.infrastructure.langgraph.runtime.core.helpers import (
    _invalidate_tui,
    _merge_workflow_runs,
    _persona_for_workflow_runs,
    _render_inline_compaction_guide,
    _task_state_for_context,
    _LLM_MAX_RETRIES,
    _LLM_TIMEOUT_MAX_RETRIES,
)

from voidx.agent.infrastructure.langgraph.runtime.session_runtime import (
    SMART_TITLE_CHARS,
    TEMPORARY_TITLE_CHARS,
    TITLE_PERSONA_USER_CHARS,
    TITLE_TIMEOUT_SECONDS,
    _collapse_whitespace,
    _message_row_title_text,
    _sanitize_generated_title,
)
from voidx.agent.infrastructure.langgraph.runtime.turn_runner import (
    RESUME_FORCE_COMPACT_MESSAGE_COUNT,
    TurnRunner,
    _resolve_recursion_limit,
)
from voidx.agent.application.compaction_service import CompactionService
from voidx.agent.domain.compaction import CompactionResult, PreflightCompactionResult
from voidx.agent.infrastructure.graph_compaction import GraphCompactionAdapter
from voidx.agent.infrastructure.langgraph.runtime.tool_executor import (
    AGENT_RESULT_PREVIEW_CHARS,
    ToolExecutorAdapter,
    _agent_result_preview,
    _make_interact_callback,
    todo_updated_event,
)
import json
from dataclasses import replace
from typing import Any, TYPE_CHECKING
from voidx.config import PermissionMode
from voidx.logging.tool_log import log_tool_event
from voidx.permission.ai_approval import is_ai_approval_candidate
from voidx.permission.service import (
    PermissionContext,
    authorize_tool_call,
    classify_tool_call,
)
from voidx.permission.context import PermissionDecision
from voidx.permission.session_rules import scoped_session_rule_for_decision
from voidx.permission.schema import Action
from voidx.permission.risk import ApprovalScope, RiskLevel
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus
from voidx.runtime.intent import PersonaName
from voidx.runtime.ui import PermissionPromptCleared, PermissionPromptShown, PermissionToolDetail
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from voidx.agent.agents import get_agent
from voidx.agent.prompts import (
    CODING_PROFILE_SPEC,
    WORKFLOW_RUNTIME,
    assemble_base_system,
    build_base_system,
    persona_prompt,
)
from voidx.agent.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
    raw_semantic_messages,
)
from voidx.agent.state import AgentState
from voidx.runtime.task_state import (
    TaskState,
    TodoRunState,
    goal_label,
    goal_type_from_join,
)
from voidx.agent.todo_state import sanitize_todo_replay_messages
from voidx.agent.tool_exchange_sanitizer import sanitize_failed_tool_exchanges
from voidx.agent.tool_filters import filter_unavailable_lsp_tools, strip_gemini_unsupported_schema_keys
from voidx.agent.infrastructure.langgraph.runtime.streaming import (
    extract_text,
    is_malformed_tool_call_response,
    stream_llm as _stream_llm,
)
from voidx.agent.infrastructure.langgraph.runtime.topology import latest_ai_message, latest_user_text, prepare_state
from voidx.agent.infrastructure.langgraph.runtime.workflow_utils import active_workflow_names
from voidx.logging.request_log import log_llm_exchange
from voidx.llm.service import resolve_protocol
from voidx.llm.usage import (
    estimate_context_tokens,
    estimate_context_tokens_with_tools,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.runtime.ui import (
    AssistantStreamCommitted,
    AssistantStreamUpdated,
    GuidanceCommitted,
    StatusFinished,
    StreamingRenderer,
)
from voidx.agent.infrastructure.langgraph.runtime.turn_control import TURN_TOOL_DEFINITION
from voidx.agent.infrastructure.langgraph.runtime.core.context import (
    rebuild_llm_messages as build_llm_context_messages,
    replacement_messages as compacted_replacement_messages,
    rerender_task_context,
    save_main_context_frame,
)
from voidx.agent.infrastructure.langgraph.runtime.core.loop import LlmLoopState, handle_llm_exception
from voidx.agent.infrastructure.langgraph.runtime.core.turn import handle_turn_control_response
from voidx.agent.infrastructure.langgraph.runtime.core.helpers import (
    _invalidate_tui,
    _merge_workflow_runs,
    _persona_for_workflow_runs,
    _render_inline_compaction_guide,
    _task_state_for_context,
    _LLM_MAX_RETRIES,
    _LLM_TIMEOUT_MAX_RETRIES,
)

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage

from voidx.agent.agents import AgentDef, get_agent
from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import CompactionCoordinator
from voidx.agent.infrastructure.langgraph.runtime.convergence import generate_fallback_summary
from voidx.agent.infrastructure.langgraph.runtime.core.helpers import (
    _interaction_mode_for_persona,
    _invalidate_tui,
    _persona_for_child_workflow,
)
from voidx.agent.infrastructure.langgraph.runtime.runtime import current_parent_tool_call_id as _current_parent_tool_call_id
from voidx.agent.infrastructure.langgraph.runtime.runtime_guards import RuntimeGuardState
from voidx.agent.infrastructure.langgraph.runtime.session_runtime import SessionRuntime
from voidx.agent.infrastructure.langgraph.runtime.streaming import stream_llm as _stream_llm
from voidx.agent.infrastructure.langgraph.runtime.subagent import run_subagent as _run_subagent
from voidx.agent.loop import LoopManager
from voidx.agent.infrastructure.langgraph.runtime.thread_context import current_thread_execution_state
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.infrastructure.langgraph.runtime.tool_executor import ToolExecutorAdapter
from voidx.agent.infrastructure.langgraph.runtime.topology import build_graph, session_date
from voidx.agent.infrastructure.langgraph.runtime.turn_metrics import TurnControlMetrics
from voidx.agent.infrastructure.langgraph.runtime.turn_runner import TurnRunner
from voidx.agent.infrastructure.langgraph.runtime.wiring import (
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
from voidx.runtime.task_state import GoalResolution, TaskState, goal_type_from_join
from voidx.agent.todo_state import apply_todo_state_to_host
from voidx.config import Config, Settings
from voidx.permission.ai_approval import AiApprovalService
from voidx.llm.instruction import InstructionService
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.service import create_chat_model
from voidx.memory.service import SessionInfo, append_subagent_event
from voidx.runtime.ui import (
    GuidanceSubmitted,
    OutputNode,
    OutputTree,
    SubagentFinished,
    SubagentStarted,
)
from voidx.runtime.ui import InteractionFrontend
from voidx.runtime.ui_port import runtime_ui_port
from voidx.skills.service import SkillRegistry, SkillService

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


def _session_runtime_for(execution: Any) -> SessionRuntime:
    return execution._session_runtime


def _turn_runner_for(execution: Any) -> TurnRunner:
    return execution._turn_runner


def _compaction_component_for(execution: Any) -> CompactionCoordinator:
    return execution._compaction_coordinator


def _tool_executor_for(execution: Any) -> ToolExecutorAdapter:
    return execution._tool_executor

def _tool_call_key(tool_call: dict[str, Any]) -> str | None:
    try:
        payload = {
            "name": str(tool_call.get("name") or ""),
            "args": tool_call.get("args") or {},
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None

def _attach_ai_approval_failures(
    decisions: list[PermissionDecision],
    candidates: list[PermissionDecision],
    result: Any,
    allowed_ids: frozenset[str],
) -> list[PermissionDecision]:
    candidate_ids = {str(decision.tool_call.get("id") or "") for decision in candidates}
    failures = {
        call_id: _ai_approval_failure_message(result, call_id)
        for call_id in candidate_ids
        if call_id and call_id not in allowed_ids
    }
    if not failures:
        return decisions
    return [
        replace(decision, ai_approval_failure=failures[call_id])
        if (call_id := str(decision.tool_call.get("id") or "")) in failures
        else decision
        for decision in decisions
    ]

def _ai_approval_failure_message(result: Any, call_id: str) -> str:
    skipped_reason = getattr(result, "skipped_reasons", {}).get(call_id, "")
    if skipped_reason:
        return f"AI approval skipped: {skipped_reason}; requesting human review."
    if getattr(result, "reason", "") == "reviewed":
        reason = getattr(result, "denied_reasons", {}).get(call_id, "")
        if reason:
            return f"AI approval denied: {reason}"
        if call_id in getattr(result, "reviewed_ids", frozenset()):
            return "AI approval denied; requesting human review."
        return "AI approval skipped: candidate was not reviewed; requesting human review."
    failure = {
        "disabled": "disabled",
        "unavailable": "unavailable",
        "invalid_response": "returned an invalid response",
        "skipped": "skipped before review",
        "timeout": "timed out",
        "connection_error": "connection error",
        "error": "internal error",
    }.get(getattr(result, "reason", ""), "unknown error")
    return f"AI approval failed: {failure}; requesting human review."

def _coerce_permission_decision(item: dict | PermissionDecision) -> PermissionDecision:
    if isinstance(item, PermissionDecision):
        return item
    classified = classify_tool_call(item)
    return PermissionDecision(
        action=Action.ASK,
        tool_call=classified.tool_call,
        name=classified.name,
        args=classified.args,
        pattern=classified.pattern,
        capability=classified.capability,
        source="compat",
    )

def _tool_call_with_approval_risk(decision: PermissionDecision, *, approved_by: str = "user") -> dict:
    if decision.risk is None or decision.risk.level == RiskLevel.BLOCKED:
        return decision.tool_call
    metadata = dict(decision.tool_call.get("metadata") or {})
    metadata["approved_risk"] = {
        "tool_name": decision.name,
        "pattern": decision.pattern,
        "risk_level": decision.risk.level.value,
        "tags": [tag.value for tag in decision.risk.tags],
        "reason": decision.risk.reason,
        **({"approved_by": approved_by} if approved_by != "user" else {}),
    }
    return {**decision.tool_call, "metadata": metadata}

def _tool_call_with_execution_approval(decision: PermissionDecision) -> dict:
    if decision.name not in {"bash", "powershell"}:
        return decision.tool_call
    if decision.risk is None or decision.risk.level == RiskLevel.NORMAL:
        return decision.tool_call
    return _tool_call_with_approval_risk(decision)

def _permission_choices(decisions: list[PermissionDecision]) -> list[tuple[str, str, str]]:
    if _all_decisions_blocked_ack(decisions):
        return [("Do not run", "n", "This command is blocked")]
    choices: list[tuple[str, str, str]] = []
    if _all_decisions_allow_scope(decisions, ApprovalScope.SESSION):
        choices.append(("Yes, always", "a", "Allow these tools for this session"))
    choices.append(("Yes", "y", "Allow this tool use once"))
    choices.append(("No", "n", "Deny these tools"))
    return choices

def _all_decisions_allow_scope(decisions: list[PermissionDecision], scope: str) -> bool:
    if not decisions:
        return False
    return all(scope in _scope_values(decision.allowed_scopes) for decision in decisions)

def _all_decisions_blocked_ack(decisions: list[PermissionDecision]) -> bool:
    return bool(decisions) and all(decision.action == Action.BLOCKED_ACK for decision in decisions)

def _scope_values(scopes: tuple[object, ...]) -> set[str]:
    return {scope.value if hasattr(scope, "value") else str(scope) for scope in scopes}

MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION = (
    "Your previous response looked like an incomplete tool call. Re-emit a valid "
    "tool call using the bound tool schema, or answer normally without tool-call markup."
)

class LangGraphExecution:
    """LangGraph-backed agent execution infrastructure."""

    @property
    def _session(self) -> SessionInfo | None:
        state = current_thread_execution_state()
        return state.session if state is not None else getattr(self, "_default_session", None)

    @_session.setter
    def _session(self, value: SessionInfo | None) -> None:
        state = current_thread_execution_state()
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

    @property
    def _task_state(self) -> TaskState:
        state = current_thread_execution_state()
        if state is not None:
            return state.task_state
        if not hasattr(self, "_default_task_state"):
            self._default_task_state = TaskState()
        return self._default_task_state

    @_task_state.setter
    def _task_state(self, value: TaskState) -> None:
        state = current_thread_execution_state()
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

    def __init__(self, config: Config, api_key: str | None, session: SessionInfo | None = None, settings: Settings | None = None):
        self.config = config
        self.api_key = api_key
        self.model = create_chat_model(api_key, config.model) if api_key else None
        self._session = session
        self._workspace = config.workspace
        self._settings = settings
        self._ai_approval = AiApprovalService()
        self._ui = runtime_ui_port
        self._gateway_session = None
        self._any_messages_sent = False
        self._startup_presenter = None

        bind_settings_to_catalog(settings)
        self._tracker, self.tools = build_tool_registry(
            settings=settings,
            config=config,
            subagent_runner=self._subagent_runner,
        )

        self._instruction = InstructionService(self._workspace, settings=settings)
        self._permission = build_permission_service(config, settings=self._settings, notifier=self._ui.ui.print)

        self._interaction_mode: InteractionMode = InteractionMode.AUTO
        self._debug: bool = False
        self._instruction.set_debug(self._debug)
        self._ui.ui.set_debug(self._debug)

        self._file_mtimes: dict[str, dict[str, int]] = {}
        self._file_read_coverage: dict[str, dict] = {}
        self._workflow_repeat_tracker: dict[str, dict[str, int]] = {}
        self._turn_node: OutputNode | None = None
        self._current_tree: OutputTree | None = None
        self._current_messages: list[BaseMessage] | None = None
        self._pending_summary: str | None = None
        self._compaction_summary: str = ""
        self._session_date: str = session_date(session)
        self._session_msg_cache: list | None = None
        self._context_cache = ContextCompilerCache()
        self._app: InteractionFrontend | None = None
        self._next_agent_id: int = 0
        self._task_state = TaskState()
        self._needs_failure_check: dict[str, dict] = {}
        self._successful_dangerous_calls: set[str] = set()
        self._successful_dangerous_calls_session_id: str | None = None
        self._runtime_guards = RuntimeGuardState()
        self._turn_metrics = TurnControlMetrics()
        self._pending_guidance: list[tuple[str, bool, Literal["user", "guard"]]] = []
        self._clear_session_tasks: set[asyncio.Task[None]] = set()
        self._title_generation: int = 0
        self._title_task: asyncio.Task[None] | None = None
        self._usage_stats, self._compaction = build_compaction_service(config)
        self._compaction_coordinator = CompactionCoordinator(self)
        self._session_runtime = SessionRuntime(self)
        self._tool_executor = ToolExecutorAdapter(self)
        self._turn_runner = TurnRunner(self)
        self._loop_manager = LoopManager(
            self,
            idle_event=self._turn_runner.idle_event,
            workspace=self._workspace,
        )
        self.tools._loop_manager = self._loop_manager
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
            model=self.model,
            model_config=self.config.model,
        )
        self._instruction.set_mcp_description_provider(self._mcp_manager.generated_descriptions)
        if TYPE_CHECKING:
            _host_contract: Any = self

    @property
    def ui(self):
        return self._ui

    @property
    def runtime_guards(self):
        return self._runtime_guards

    async def apply_settings_update(self, settings: Settings) -> None:
        await self._apply_settings_update(settings)

    @property
    def plan_mode(self) -> bool:
        return self._plan_mode

    @property
    def slash(self):
        return self._slash

    @property
    def gateway_session(self):
        return self._gateway_session

    @gateway_session.setter
    def gateway_session(self, value) -> None:
        self._gateway_session = value

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
    def app(self) -> InteractionFrontend | None:
        """The interactive TUI app, if one is running."""
        return self._app

    @app.setter
    def app(self, value: InteractionFrontend | None) -> None:
        self._app = value

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

    async def _apply_settings_update(self, settings: Settings) -> None:
        profile = await settings.resolve_profile()
        new_config = await settings.build_config(profile=profile)
        new_config.workspace = self._workspace

        self.clear_successful_dangerous_calls()
        self._settings = settings
        self.config = new_config
        self.api_key = profile.api_key if profile is not None else None
        self.model = create_chat_model(self.api_key, self.config.model) if self.api_key else None
        if self._mcp_manager is not None:
            self._mcp_manager.set_description_model(self.model)

        bind_settings_to_catalog(settings)
        self._tracker, self.tools = build_tool_registry(
            settings=settings,
            config=self.config,
            subagent_runner=self._subagent_runner,
        )
        old_permission = getattr(self, "_permission", None)
        self._permission = build_permission_service(self.config, settings=settings, notifier=self._ui.ui.print)
        if old_permission is not None and hasattr(old_permission, "ai_approval_count"):
            self._permission.ai_approval_count = old_permission.ai_approval_count
        self._tool_executor = ToolExecutorAdapter(self)
        if hasattr(self, "_loop_manager"):
            self.tools._loop_manager = self._loop_manager

        context_limit = self._compaction.context_limit
        updated_usage_stats, updated_compaction = build_compaction_service(self.config)
        context_limit = updated_usage_stats.context_limit or context_limit
        self._usage_stats.context_limit = context_limit
        self._compaction.context_limit = updated_compaction.context_limit
        self._compaction.output_token_max = updated_compaction.output_token_max
        self._compaction.soft_ratio = updated_compaction.soft_ratio
        self._compaction.post_target_ratio = updated_compaction.post_target_ratio

        app = getattr(self, "_app", None)
        status = getattr(app, "status", None)
        if status is not None:
            status.provider = self.config.model.provider
            status.model = self.config.model.model
            status.context_limit = context_limit
            status.reasoning_effort = self.config.model.reasoning_effort or "xhigh"

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
    def loop_manager(self) -> LoopManager:
        return self._loop_manager

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

    def _turn_control_enabled(self) -> bool:
        return True

    def set_task_state(self, task_state: TaskState) -> None:
        self._task_state = task_state

    def can_submit_guidance(self) -> bool:
        return True

    def submit_guidance(
        self,
        text: str,
        *,
        source: Literal["user", "guard"] = "user",
    ) -> bool:
        guidance = " ".join(text.strip().split())
        if not guidance:
            return False
        truncated = False
        if len(guidance) > GUIDANCE_MAX_CHARS:
            guidance = guidance[:GUIDANCE_MAX_CHARS].rstrip()
            truncated = True
        if source == "user" and self._ui.via_events():
            if not self._ui.events.emit_direct(
                GuidanceSubmitted(text=guidance, truncated=truncated)
            ):
                return False
        self._pending_guidance.append((guidance, truncated, source))
        return True

    def _drain_pending_guidance(self) -> list[tuple[HumanMessage, bool, Literal["user", "guard"]]]:
        messages: list[tuple[HumanMessage, bool, Literal["user", "guard"]]] = []
        while self._pending_guidance:
            entry = self._pending_guidance.pop(0)
            if len(entry) == 2:
                text, truncated = entry
                source: Literal["user", "guard"] = "user"
            else:
                text, truncated, source = entry
            messages.append((
                HumanMessage(
                    content=text,
                    additional_kwargs={GUIDANCE_MARKER: True},
                ),
                truncated,
                source,
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

    async def run_synthetic_turn(
        self,
        text: str,
        *,
        display_text: str | None = None,
        context: TurnExecutionContext | None = None,
    ) -> None:
        if context is None:
            state = current_thread_execution_state()
            if state is None or state.turn_context is None:
                raise RuntimeError("synthetic turn requires bound TurnExecutionContext")
            context = state.turn_context
        await self.run_turn(text, display_text=display_text, context=context)

    async def clear_current_session(self) -> None:
        await self._loop_manager.cleanup()
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
        await self._loop_manager.cleanup()
        self._invalidate_session_title_generation()
        self._session = session
        self._workspace = session.workspace
        self.config.workspace = session.workspace
        self._loop_manager.set_workspace(self._workspace)
        self._session_date = session_date(session)
        self._session_msg_cache = None
        self._context_cache = ContextCompilerCache()
        await self.restore_runtime_state()
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
        goal_resolution: GoalResolution,
        result_contract: Any,
        *,
        permission_snapshot=None,
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
            goal_type=goal_type,
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
        result = ""
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
                "permission_snapshot": permission_snapshot,
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
                    summary=result if ok else "",
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
        from voidx.agent.domain.turn import TurnPhase
        from voidx.agent.infrastructure.langgraph.state_mapper import LangGraphStateMapper

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
    ) -> None:
        if not isinstance(context, TurnExecutionContext):
            raise TypeError("run_turn requires TurnExecutionContext")
        await _turn_runner_for(self).run_once(
            user_text,
            display_text=display_text,
            context=context,
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
        service = CompactionService(
            GraphCompactionAdapter(
                coordinator,
                run_compaction_agent=self._run_compaction_agent,
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
            run_compaction_agent=self._run_compaction_agent,
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
            run_compaction_agent=self._run_compaction_agent,
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
        if getattr(self, "_successful_dangerous_calls_session_id", None) != session_id:
            self._successful_dangerous_calls.clear()
            self._successful_dangerous_calls_session_id = session_id
        state_context = current_thread_execution_state()
        chat_tool_view = getattr(state_context, "tool_policy", None) if state_context else None
        if chat_tool_view is not None:
            approved: list[dict] = []
            denied: list[tuple[dict, str]] = []
            for tool_call in tool_calls:
                args = tool_call.get("args", {}) or {}
                raw_path = args.get("path") or args.get("file") or args.get("directory")
                decision = chat_tool_view.check(
                    str(tool_call.get("name", "")),
                    path=Path(raw_path) if raw_path else None,
                )
                if decision.allowed:
                    approved.append(tool_call)
                else:
                    denied.append((tool_call, f"Tool denied: {decision.reason}"))
            return approved, denied
        approved: list[dict] = []
        denied: list[tuple[dict, str]] = []
        need_ask: list[PermissionDecision] = []
        context = PermissionContext.from_service(
            self._permission,
            workspace=self._workspace,
            interaction_mode=interaction_mode,
            plan_mode=plan_mode,
        )

        for tc in tool_calls:
            decision = authorize_tool_call(tc, context)
            if (
                decision.action == Action.ASK
                and decision.risk is not None
                and decision.risk.level == RiskLevel.DANGEROUS
                and getattr(self._permission, "permission_mode", "") == PermissionMode.AI_APPROVAL.value
                and _tool_call_key(tc) in getattr(self, "_successful_dangerous_calls", set())
            ):
                approved.append(_tool_call_with_approval_risk(decision, approved_by="cached"))
            elif decision.action == Action.ALLOW:
                approved_call = _tool_call_with_execution_approval(decision)
                approved.append(approved_call)
                if decision.failure_check:
                    self._needs_failure_check[approved_call.get("id", "")] = approved_call
            elif decision.action == Action.DEFER:
                approved.append(decision.tool_call)
            elif decision.action == Action.DENY:
                denied.append((decision.tool_call, decision.reason))
            elif decision.action == Action.BLOCKED_ACK:
                need_ask.append(decision)
            else:
                need_ask.append(decision)

        if need_ask:
            await self._ask_and_apply_permission(need_ask, approved, denied)

        return approved, denied

    async def _ask_and_apply_permission(
        self: Any,
        need_ask: list[PermissionDecision],
        approved: list[dict],
        denied: list[tuple[dict, str]],
    ) -> None:
        blocked = [decision for decision in need_ask if decision.action == Action.BLOCKED_ACK]
        ai_allowed: list[PermissionDecision] = []
        if (
            getattr(self._permission, "permission_mode", "") == PermissionMode.AI_APPROVAL.value
            and getattr(self, "_settings", None) is not None
            and getattr(self, "_ai_approval", None) is not None
        ):
            candidates = [
                decision for decision in need_ask
                if is_ai_approval_candidate(decision)
            ]
            if candidates:
                result = await self._ai_approval.review(candidates, self._settings)
                allowed_ids = result.allowed_ids if result.reason == "reviewed" else frozenset()
                for decision in candidates:
                    if decision.tool_call.get("id") in allowed_ids:
                        ai_allowed.append(decision)
                        approved.append(_tool_call_with_approval_risk(decision, approved_by="ai"))
                        self._notice_permission_result(f"AI 审批: allow {decision.name}")
                        if hasattr(self._permission, "inc_ai_approval_count"):
                            self._permission.inc_ai_approval_count()
                need_ask = _attach_ai_approval_failures(need_ask, candidates, result, allowed_ids)
            if ai_allowed:
                if self._ui.via_events():
                    from voidx.runtime.ui import RefreshRequested
                    await self._ui.events.emit(RefreshRequested())
                need_ask = [decision for decision in need_ask if decision not in ai_allowed]

        approvable = [decision for decision in need_ask if decision.action != Action.BLOCKED_ACK]

        if blocked:
            await self._ask_tool_permission(blocked)
            if self._ui.via_events():
                await self._ui.events.emit(PermissionPromptCleared())
            for decision in blocked:
                denied.append((decision.tool_call, decision.reason or "Blocked command"))

        if not approvable:
            return

        choice = await self._ask_tool_permission(approvable)
        if choice is None:
            choice = "n"

        if self._ui.via_events():
            await self._ui.events.emit(PermissionPromptCleared())

        tool_calls = [_tool_call_with_approval_risk(decision) for decision in approvable]
        if choice == "a" and _all_decisions_allow_scope(approvable, ApprovalScope.SESSION):
            for decision in approvable:
                self._permission.allow_silent(scoped_session_rule_for_decision(decision))
            approved.extend(tool_calls)
        elif choice == "y":
            approved.extend(tool_calls)
        else:
            self._notice_permission_result(f"{len(need_ask)} tools denied")
            for tc in tool_calls:
                denied.append((tc, f"User denied: {tc['name']}"))

    async def _ask_tool_permission(self: Any, tool_calls: list[dict] | list[PermissionDecision]) -> str | None:
        decisions = [_coerce_permission_decision(item) for item in tool_calls]
        raw_tool_calls = [decision.tool_call for decision in decisions]
        tool_list = ", ".join(t["name"] for t in raw_tool_calls)
        choices = _permission_choices(decisions)
        details = [item.model_dump(mode="json") for item in self._permission_tool_details(decisions)]

        if self._ui.via_events():
            await self._ui.events.emit(PermissionPromptShown(
                prompt=f"Allow tools: {tool_list}?",
                choices=choices,
                tools=self._permission_tool_details(decisions),
            ))

        if not self._app:
            self._ui.ui.print("")
            self._ui.ui.print(f"  [yellow]Allow tools: [bold]{tool_list}[/bold]?[/yellow]")

        if self._app:
            return await self._app.ask_choice("Allow tool use?", choices, details=details)
        return "n"

    def _show_permission_output(self: Any, message: str) -> bool:
        dock = getattr(getattr(self, "_ui", None), "dock", None)
        append = getattr(dock, "append_message", None)
        if not callable(append):
            return False
        append(message)
        return True

    def _notice_permission_result(self: Any, message: str) -> None:
        log_tool_event("permission_notice", message=message)

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

    def _permission_tool_details(self: Any, decisions: list[PermissionDecision]) -> list[PermissionToolDetail]:
        details: list[PermissionToolDetail] = []
        for decision in decisions:
            details.append(PermissionToolDetail(
                name=decision.name,
                pattern=decision.pattern,
                args=decision.args,
                risk=decision.risk.model_dump(mode="json") if decision.risk is not None else None,
                allowed_scopes=tuple(scope.value if hasattr(scope, "value") else str(scope) for scope in decision.allowed_scopes),
                default_scope=decision.default_scope.value if hasattr(decision.default_scope, "value") else decision.default_scope,
                ai_approval_failure=decision.ai_approval_failure,
            ))
        return details

    async def _prepare_with_stream(self, state: AgentState) -> dict:
        base = prepare_state(state)
        agent_id = "voidx"
        runtime_persona = state.get("persona", "coordinate")
        self._current_agent = get_agent(agent_id)
        rendered_persona_prompt = persona_prompt() if self._current_agent else ""

        state_context = current_thread_execution_state()
        active_profile = getattr(state_context, "runtime_profile", None) if state_context else None
        prompt_policy = getattr(active_profile, "prompt_policy", None)
        persona_prompt_value = (
            prompt_policy.persona_prompt
            if prompt_policy is not None and prompt_policy.persona_prompt is not None
            else rendered_persona_prompt
        )
        workflow_runtime_value = (
            prompt_policy.workflow_runtime
            if prompt_policy is not None and prompt_policy.workflow_runtime is not None
            else WORKFLOW_RUNTIME
        )
        profile_directive_value = (
            prompt_policy.profile_directive
            if prompt_policy is not None and prompt_policy.profile_directive is not None
            else ""
        )
        task_state_suppressed = (
            prompt_policy is not None and prompt_policy.task_state_section == ""
        )
        base_system_spec = (
            prompt_policy.base_system_spec
            if prompt_policy is not None and prompt_policy.base_system_spec is not None
            else CODING_PROFILE_SPEC
        )
        state_context = current_thread_execution_state()
        active_tool_view = getattr(state_context, "tool_policy", None) if state_context else None
        available_tools = (
            set(active_tool_view.bound_tool_ids)
            if active_tool_view is not None
            else None
        )
        base_system = assemble_base_system(
            base_system_spec,
            available_tools=available_tools,
        )

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
            goal_type=goal_type_from_join(workflow_start),
            scope=goal_label(current_goal) or current_user_text,
            active_names=active_workflow_names(existing_workflow_runs),
            workflow_start=workflow_start,
        )
        workflow_runs = _merge_workflow_runs(
            existing_workflow_runs,
            workflow_context.runs,
        )
        runtime_persona = _persona_for_workflow_runs(workflow_runs, fallback=runtime_persona)
        summary = self._pending_summary or self._compaction_summary
        self._pending_summary = None

        self._last_context_builder = RuntimeContextBuilder(
            config=self.config,
            workspace=state.get("workspace", "."),
            base_system_prompt=build_base_system(
                self.config.user_profile.language,
                base_system=base_system,
            ),
            workflow_runtime=workflow_runtime_value,
            persona_prompt=persona_prompt_value,
            persona=runtime_persona,
            interaction_mode=interaction_mode,
            instructions=instructions,
            workflow_runs=workflow_runs,
            active_workflow_summaries=workflow_context.active,
            summary=summary,
            task_state=task_state,
            session_date=self._session_date,
            turn_state=state.get("turn_state", "initial"),
            profile_directive=profile_directive_value,
            suppress_task_state=task_state_suppressed,
        )
        context, self._context_cache = self._last_context_builder.build_incremental(self._context_cache)
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

        interaction_mode_value = state.get("interaction_mode") or (
            InteractionMode.PLAN.value if state.get("plan_mode", False) else self._interaction_mode.value
        )
        turn_state = str(state.get("turn_state") or "initial")
        tool_defs = self.tools.tools_for_llm()
        state_context = current_thread_execution_state()
        chat_tool_view = getattr(state_context, "tool_policy", None) if state_context else None
        if chat_tool_view is not None:
            tool_defs = [tool for tool in tool_defs if chat_tool_view.allows(tool.get("name", ""))]
        turn_control_active = self._turn_control_enabled()
        if turn_control_active:
            tool_defs = [*tool_defs, TURN_TOOL_DEFINITION]
        runtime_task_state = _task_state_for_context(
            state.get("task_state"),
            getattr(self, "_task_state", None),
        )
        tool_defs = filter_unavailable_lsp_tools(tool_defs, getattr(self, "_lsp_manager", None))
        tool_defs = strip_gemini_unsupported_schema_keys(tool_defs, resolve_protocol(self.config.model))

        guidance_pairs = self._drain_pending_guidance()
        guidance_messages = [msg for msg, _, _ in guidance_pairs]
        if self._ui.via_events() and guidance_pairs:
            user_guidance = [
                str(msg.content)
                for msg, _, source in guidance_pairs
                if source == "user"
            ]
            if user_guidance:
                self._ui.events.emit_direct(
                    GuidanceCommitted(
                        text="\n".join(user_guidance),
                        truncated=any(
                            truncated for _, truncated, source in guidance_pairs
                            if source == "user"
                        ),
                        source="user",
                    )
                )
        state_messages = sanitize_todo_replay_messages(
            list(state["messages"]),
            preserve_latest_tool_exchange=True,
        )
        state_messages = sanitize_failed_tool_exchanges(
            state_messages,
            preserve_latest=True,
            preserve_rounds=2,
        )
        compaction_happened = False
        raw_todo_state = (
            state["todo_state"]
            if "todo_state" in state
            else getattr(getattr(self, "_task_state", None), "todo_state", None)
        )
        if raw_todo_state is not None:
            try:
                runtime_task_state.todo_state = (
                    raw_todo_state
                    if isinstance(raw_todo_state, TodoRunState)
                    else TodoRunState.model_validate(raw_todo_state)
                )
            except (TypeError, ValueError):
                runtime_task_state.todo_state = None

        def rebuild_llm_messages(
            messages: list[BaseMessage],
            *,
            allow_inline_compaction: bool,
        ) -> tuple[list[BaseMessage], list[HumanMessage], bool]:
            return build_llm_context_messages(
                messages,
                guidance_messages,
                allow_inline_compaction=allow_inline_compaction,
                compaction_happened=compaction_happened,
                inline_compaction_guide_for=self._inline_compaction_guide_for,
            )

        async def save_context_frame(
            messages: list[BaseMessage],
            token_estimate: int,
            convergence_messages: list[HumanMessage],
            convergence_forced: bool,
        ) -> None:
            await save_main_context_frame(
                session=self._session,
                user_message_id=state.get("user_message_id"),
                persona=persona,
                provider=self.config.model.provider,
                model=self.config.model.model,
                messages=messages,
                token_estimate=token_estimate,
                step=step,
                tool_count=len(tool_defs),
                convergence_messages=convergence_messages,
                convergence_forced=convergence_forced,
            )

        def replacement_messages(assistant_msg: AIMessage) -> list[BaseMessage]:
            return compacted_replacement_messages(
                assistant_msg,
                compaction_happened=compaction_happened,
                state_messages=state_messages,
            )

        def _rerender_task_context(messages: list[BaseMessage], new_turn_state: str, task_state: TaskState | None = None) -> list[BaseMessage]:
            return rerender_task_context(
                getattr(self, "_last_context_builder", None),
                messages,
                new_turn_state,
                task_state,
            )

        def estimate_llm_context_tokens(messages: list[BaseMessage]) -> int:
            return estimate_context_tokens_with_tools(
                messages,
                tool_defs,
                self.config.model.model,
            )

        async def apply_compaction_result(result: CompactionResult) -> tuple[list[BaseMessage], list[HumanMessage], bool, int]:
            nonlocal compaction_happened, state_messages, runtime_task_state
            compaction_happened = True
            state_messages = list(result.live_messages)
            if result.summary:
                reprepare_state = {
                    **state,
                    "messages": state_messages,
                    "task_state": runtime_task_state.model_dump(mode="json"),
                }
                prepared = await self._prepare_with_stream(reprepare_state)
                runtime_task_state = _task_state_for_context(
                    prepared.get("task_state"),
                    runtime_task_state,
                )
            rebuilt, conv_messages, conv_forced = rebuild_llm_messages(
                state_messages,
                allow_inline_compaction=False,
            )
            rebuilt_tokens = estimate_llm_context_tokens(rebuilt)
            self._usage_stats.update_context(rebuilt_tokens)
            return rebuilt, conv_messages, conv_forced, rebuilt_tokens

        llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(
            state_messages,
            allow_inline_compaction=getattr(self.config, "inline_compaction_enabled", False),
        )

        persona = state.get("persona", "coordinate")
        if self._debug:
            self._ui.ui.print()

        # ── LLM call with retry ────────────────────────────────────────
        loop = LlmLoopState(
            context_tokens=estimate_llm_context_tokens(llm_messages),
        )
        self._usage_stats.update_context(loop.context_tokens)
        if self._compaction.is_overflow({"total": loop.context_tokens}):
            result, _preflight_result = await self._preflight_compact_if_needed(
                state_messages,
                force=True,
                reason="hard_threshold",
                ask=False,
            )
            if result is not None:
                llm_messages, convergence_messages, convergence_forced, context_tokens = (
                    await apply_compaction_result(result)
                )
                loop.context_tokens = context_tokens

        await save_context_frame(llm_messages, loop.context_tokens, convergence_messages, convergence_forced)
        max_retries = _LLM_MAX_RETRIES
        while True:
            try:
                renderer = StreamingRenderer(
                    self._ui.console,
                    debug=self._debug,
                    headless=loop.turn_prompt_active,
                )
                model_with_tools = self.model.bind_tools(tool_defs) if tool_defs else self.model
                assistant_msg = await _stream_llm(
                    model_with_tools,
                    llm_messages,
                    renderer,
                    resolve_protocol(self.config.model),
                )
                log_llm_exchange(
                    llm_messages,
                    assistant_msg,
                    model=self.config.model.model,
                    provider=self.config.model.provider,
                    step=step,
                    session_id=self._session.id if self._session else None,
                    enabled=self.config.log_llm_exchange,
                )
                self._usage_stats.record_call(
                    extract_token_usage(assistant_msg),
                    fallback_input_tokens=loop.context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, self.config.model.model),
                    messages=llm_messages,
                    model=self.config.model.model,
                    cache_key=f"{self.config.model.provider}/{self.config.model.model}",
                )
                if is_malformed_tool_call_response(assistant_msg):
                    if loop.malformed_tool_call_attempts < 1:
                        loop.malformed_tool_call_attempts += 1
                        llm_messages = [
                            *llm_messages,
                            HumanMessage(
                                content=MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION,
                                additional_kwargs={GUIDANCE_MARKER: True},
                            ),
                        ]
                        loop.context_tokens = estimate_llm_context_tokens(llm_messages)
                        self._usage_stats.update_context(loop.context_tokens)
                        continue
                    if loop.malformed_tool_call_attempts < 2 and compaction_happened:
                        result, _preflight_result = await self._preflight_compact_if_needed(
                            state_messages,
                            force=True,
                            reason="malformed_tool_call",
                            ask=False,
                        )
                        loop.malformed_tool_call_attempts += 1
                        if result is not None:
                            llm_messages, convergence_messages, convergence_forced, context_tokens = (
                                await apply_compaction_result(result)
                            )
                            loop.context_tokens = context_tokens
                            llm_messages = [
                                *llm_messages,
                                HumanMessage(
                                    content=MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION,
                                    additional_kwargs={GUIDANCE_MARKER: True},
                                ),
                            ]
                            loop.context_tokens = estimate_llm_context_tokens(llm_messages)
                            self._usage_stats.update_context(loop.context_tokens)
                            await save_context_frame(
                                llm_messages,
                                loop.context_tokens,
                                convergence_messages,
                                convergence_forced,
                            )
                            continue
                    failure_msg = AIMessage(
                        content="LLM call failed: model returned an invalid or incomplete tool call."
                    )
                    return {
                        "messages": replacement_messages(failure_msg),
                        "step_count": step,
                        "should_continue": False,
                    }
                if self._debug or not assistant_msg.tool_calls:
                    self._ui.ui.print()
                if loop.retry_status_active and self._ui.via_events():
                    await self._ui.events.emit(StatusFinished(status_id="llm:retry"))

                if turn_control_active:
                    turn_result = await handle_turn_control_response(
                        graph=self,
                        assistant_msg=assistant_msg,
                        llm_messages=llm_messages,
                        loop=loop,
                        turn_state=turn_state,
                        runtime_task_state=runtime_task_state,
                        state_messages=state_messages,
                        interaction_mode_value=interaction_mode_value,
                        estimate_tokens=estimate_llm_context_tokens,
                        rerender_task_context=_rerender_task_context,
                    )
                    llm_messages = turn_result.llm_messages
                    turn_state = turn_result.turn_state
                    runtime_task_state = turn_result.runtime_task_state
                    if turn_result.action == "retry":
                        self._usage_stats.update_context(turn_result.context_tokens)
                        continue
                    if turn_result.action == "fail":
                        return {
                            "messages": replacement_messages(turn_result.failure_msg),
                            "step_count": step + 1,
                            "should_continue": False,
                        }
                    if turn_result.action == "break":
                        break

                break
            except Exception as e:
                from voidx.agent.infrastructure.langgraph.runtime.core.helpers import _classify_llm_error

                kind = _classify_llm_error(e)

                retry_result = await handle_llm_exception(
                    ui=self._ui,
                    loop=loop,
                    error=e,
                    kind=kind,
                    max_retries=max_retries,
                    timeout_max_retries=_LLM_TIMEOUT_MAX_RETRIES,
                )
                if retry_result.action == "overflow":
                    result, _preflight_result = await self._preflight_compact_if_needed(
                        state_messages,
                        force=True,
                        reason="provider_overflow",
                        ask=False,
                    )
                    if result is not None:
                        llm_messages, convergence_messages, convergence_forced, context_tokens = (
                            await apply_compaction_result(result)
                        )
                        loop.context_tokens = context_tokens
                        await save_context_frame(
                            llm_messages,
                            loop.context_tokens,
                            convergence_messages,
                            convergence_forced,
                        )
                        continue
                if retry_result.action == "retry":
                    continue
                if retry_result.action == "fail":
                    return {
                        "messages": [],
                        "step_count": step,
                        "should_continue": False,
                    }

        final_msg = loop.terminal_msg if loop.terminal_msg is not None else assistant_msg
        if loop.terminal_msg is not None and not loop.terminal_msg_visible:
            final_text = extract_text(final_msg).strip()
            if final_text:
                if self._ui.via_events():
                    await self._ui.events.emit(AssistantStreamUpdated(text=final_text, phase="text"))
                    await self._ui.events.emit(AssistantStreamCommitted())
                else:
                    self._ui.ui.print(final_text)
        return {
            "messages": replacement_messages(final_msg),
            "step_count": step + 1,
            "convergence_forced": convergence_forced,
            "turn_state": turn_state,
            "task_state": runtime_task_state.model_dump(mode="json"),
        }

    def _router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "execute"
        return "end"

    async def _finalize(self, state: AgentState) -> dict:
        from voidx.agent.infrastructure.langgraph.runtime.convergence import generate_fallback_summary

        if not state.get("convergence_forced"):
            return {}
        last = latest_ai_message(state.get("messages", []))
        if isinstance(last, AIMessage) and not last.tool_calls:
            if len(extract_text(last).strip()) >= 20:
                return {}
        return {"messages": [AIMessage(content=generate_fallback_summary(state))]}
