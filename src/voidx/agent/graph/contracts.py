"""Type contracts for graph composition hosts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.messages import BaseMessage

from voidx.agent.runtime_context import ContextCompilerCache, InteractionMode
from voidx.agent.state import AgentState
from voidx.agent.task_state import TaskRun, TaskState
from voidx.config import Config, Settings
from voidx.llm.compaction import CompactionService
from voidx.llm.instruction import InstructionService
from voidx.llm.usage import UsageStats
from voidx.memory.session import SessionInfo
from voidx.permission.service import PermissionService
from voidx.runtime.ui_port import AgentUiPort
from voidx.tools.registry import ToolRegistry
from voidx.tools.task_tracker import TaskTracker

if TYPE_CHECKING:
    from voidx.agent.graph.compaction_coordinator import CompactionResult, GraphCompactionCoordinator
    from voidx.agent.graph.session_runtime import GraphSessionRuntime
    from voidx.agent.graph.tool_executor import GraphToolExecutor
    from voidx.agent.graph.turn_runner import GraphTurnRunner


class GraphCompactionHost(Protocol):
    """Host surface required by compaction coordinator and proxy mixin."""

    config: Config
    model: Any | None
    _ui: AgentUiPort
    _session: SessionInfo | None
    _app: Any | None
    _debug: bool
    _usage_stats: UsageStats
    _compaction: CompactionService
    _compaction_coordinator: GraphCompactionCoordinator
    _in_turn_compaction_count: int
    _pending_summary: str | None
    _compaction_summary: str
    _session_msg_cache: list[Any] | None

    async def _persist_runtime_state(self) -> None: ...
    async def _maybe_compact(
        self,
        messages: list[BaseMessage],
        session_msgs: list[Any] | None = None,
        *,
        force: bool = False,
        ask: bool = True,
    ) -> tuple[list[BaseMessage] | None, str | None]: ...
    async def _in_turn_compact(
        self,
        messages: list[BaseMessage],
    ) -> CompactionResult | None: ...
    async def _ask_compact(self, total_tokens: int) -> bool: ...
    async def _persist_compaction(self, head_messages: list[BaseMessage]) -> None: ...
    async def _compact_session_history(self, *, force: bool = True) -> bool: ...
    async def _run_compaction_agent(
        self,
        head_messages: list[BaseMessage],
        previous_summary: str | None,
    ) -> str | None: ...


class GraphToolExecutionHost(Protocol):
    """Host surface required by tool execution coordinator and proxy mixin."""

    config: Config
    tools: ToolRegistry
    _ui: AgentUiPort
    _session: SessionInfo | None
    _workspace: str
    _app: Any | None
    _debug: bool
    _file_mtimes: dict[str, float]
    _turn_node: Any | None
    _current_messages: list[BaseMessage] | None
    _permission: PermissionService
    _mcp_manager: Any
    _lsp_manager: Any
    _needs_failure_check: dict[str, dict]
    _tool_executor: GraphToolExecutor

    async def _authorize_tool_calls(
        self,
        tool_calls: list[dict],
        agent_name: str,
        plan_mode: bool,
        session_id: str,
        interaction_mode: str | None = None,
        skill_runs: object = (),
    ) -> tuple[list[dict], list[tuple[dict, str]]]: ...
    def _notify_tool_failure(self, tc: dict, result: Any) -> None: ...
    def _clear_failure_check(self, cid: str) -> None: ...
    async def _execute_tools(self, state: AgentState) -> dict: ...
    @staticmethod
    def _tool_result_ok(result: Any) -> bool: ...


class GraphPermissionHost(Protocol):
    """Host surface required by permission mixin."""

    _ui: AgentUiPort
    _workspace: str
    _app: Any | None
    _permission: PermissionService
    _needs_failure_check: dict[str, dict]

    async def _authorize_tool_calls(
        self,
        tool_calls: list[dict],
        agent_name: str,
        plan_mode: bool,
        session_id: str,
        interaction_mode: str | None = None,
        skill_runs: object = (),
    ) -> tuple[list[dict], list[tuple[dict, str]]]: ...
    async def _ask_and_apply_permission(
        self,
        need_ask: list[dict],
        approved: list[dict],
        denied: list[tuple[dict, str]],
    ) -> None: ...
    async def _ask_tool_permission(self, tool_calls: list[dict]) -> str | None: ...
    def _notice_permission_result(self, message: str) -> None: ...
    def _notify_tool_failure(self, tc: dict, result: Any) -> None: ...
    def _show_permission_output(self, message: str) -> bool: ...
    def _clear_failure_check(self, cid: str) -> None: ...
    def _permission_tool_details(self, tool_calls: list[dict]) -> list[Any]: ...


class GraphRunLoopHost(Protocol):
    """Host surface required by run loop, turn, session, transcript, and title components."""

    config: Config
    model: Any | None
    graph: Any
    _ui: AgentUiPort
    _session: SessionInfo | None
    _workspace: str
    _settings: Settings | None
    _permission: PermissionService
    _interaction_mode: InteractionMode
    _debug: bool
    _turn_node: Any | None
    _current_tree: Any | None
    _pending_summary: str | None
    _compaction_summary: str
    _session_msg_cache: list[Any] | None
    _context_cache: ContextCompilerCache
    _app: Any | None
    _usage_stats: UsageStats
    _compaction: CompactionService
    _session_runtime: GraphSessionRuntime
    _turn_runner: GraphTurnRunner
    _task_state: TaskState
    _task_run: TaskRun
    _clear_session_tasks: set[asyncio.Task[None]]
    _title_generation: int
    _title_task: asyncio.Task[None] | None
    _mcp_manager: Any
    _lsp_manager: Any
    _slash: Any
    _any_messages_sent: bool
    _pending_guidance: list[str]

    @property
    def _plan_mode(self) -> bool: ...

    async def _show_startup(
        self,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> None: ...
    def _startup_title(self) -> str: ...
    async def _show_update_check_if_needed(self) -> None: ...
    async def _handle_web_command(self, app: Any, command: Any) -> None: ...
    async def _handle_user_input(self, app: Any, user_input: str) -> tuple[bool, str | None]: ...
    async def _run_once(self, user_text: str, *, display_text: str | None = None) -> None: ...
    async def _dispatch_slash(self, inp: str) -> bool: ...
    async def _restore_runtime_state(self) -> None: ...
    async def _persist_runtime_state(self) -> None: ...
    async def _clear_runtime_state(self) -> None: ...
    def _reset_runtime_state_memory(self) -> None: ...
    def _invalidate_session_title_generation(self) -> None: ...
    def _temporary_session_title(self, text: str) -> str: ...
    def _schedule_session_title_generation(
        self,
        session_id: str,
        first_user_text: str,
        temporary_title: str,
    ) -> None: ...
    async def regenerate_session_title(self) -> bool: ...
    async def _delete_empty_current_session(self) -> None: ...
    async def _persist_transcript_snapshot(self) -> None: ...
    async def _restore_transcript_snapshot(self, *, append: bool = False) -> bool: ...
    async def _maybe_compact(
        self,
        messages: list[BaseMessage],
        session_msgs: list[Any] | None = None,
        *,
        force: bool = False,
        ask: bool = True,
    ) -> tuple[list[BaseMessage] | None, str | None]: ...


class GraphComponentHost(
    GraphRunLoopHost,
    GraphCompactionHost,
    GraphToolExecutionHost,
    GraphPermissionHost,
    Protocol,
):
    """Composite host surface used only for whole-graph wiring checks."""

    api_key: str | None
    _instruction: InstructionService
    _tracker: TaskTracker
