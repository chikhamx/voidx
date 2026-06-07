"""Type contracts for graph component mixins.

VoidXGraph currently hosts several mixins that share runtime state through
private attributes. These protocols make that shared surface explicit without
changing the runtime inheritance model.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

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
from voidx.tools.registry import ToolRegistry
from voidx.tools.task_tracker import TaskTracker


class GraphComponentHost(Protocol):
    """Shared host surface required by the graph component mixins."""

    config: Config
    api_key: str | None
    model: Any | None
    graph: Any
    tools: ToolRegistry

    _session: SessionInfo | None
    _workspace: str
    _settings: Settings | None
    _instruction: InstructionService
    _permission: PermissionService
    _tracker: TaskTracker

    _interaction_mode: InteractionMode
    _debug: bool
    _file_mtimes: dict[str, float]
    _turn_node: Any | None
    _current_tree: Any | None
    _current_messages: list[BaseMessage] | None
    _sub_buffers: dict[str, list[BaseMessage]]
    _pending_summary: str | None
    _compaction_summary: str
    _session_msg_cache: list[Any] | None
    _context_cache: ContextCompilerCache
    _app: Any | None
    _usage_stats: UsageStats
    _compaction: CompactionService
    _task_state: TaskState
    _task_run: TaskRun
    _clear_session_tasks: set[asyncio.Task[None]]

    _mcp_manager: Any
    _lsp_manager: Any
    _slash: Any

    _any_messages_sent: bool
    _needs_failure_check: dict[str, dict]

    @property
    def _plan_mode(self) -> bool: ...

    async def _show_startup(
        self,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> None: ...
    def _startup_title(self) -> str: ...
    async def _handle_web_command(self, app: Any, command: Any) -> None: ...
    async def _handle_user_input(self, app: Any, user_input: str) -> tuple[bool, str | None]: ...
    async def _run_once(self, user_text: str, *, display_text: str | None = None) -> None: ...
    async def _dispatch_slash(self, inp: str) -> bool: ...
    async def _restore_runtime_state(self) -> None: ...
    async def _persist_runtime_state(self) -> None: ...
    async def _clear_runtime_state(self) -> None: ...
    def _reset_runtime_state_memory(self) -> None: ...
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
    async def _ask_compact(self, total_tokens: int) -> bool: ...
    async def _persist_compaction(self, head_messages: list[BaseMessage]) -> None: ...
    async def _compact_session_history(self, *, force: bool = True) -> bool: ...
    async def _run_compaction_agent(
        self,
        head_messages: list[BaseMessage],
        previous_summary: str | None,
    ) -> str | None: ...

    async def _authorize_tool_calls(
        self,
        tool_calls: list[dict],
        agent_name: str,
        plan_mode: bool,
        session_id: str,
        interaction_mode: str | None = None,
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

    async def _execute_tools(self, state: AgentState) -> dict: ...
    @staticmethod
    def _tool_result_ok(result: Any) -> bool: ...


class GraphRunLoopHost(GraphComponentHost, Protocol):
    """Host requirements for GraphRunLoopMixin."""


class GraphCompactionHost(GraphComponentHost, Protocol):
    """Host requirements for GraphCompactionMixin."""


class GraphToolExecutionHost(GraphComponentHost, Protocol):
    """Host requirements for GraphToolExecutionMixin."""


class GraphPermissionHost(GraphComponentHost, Protocol):
    """Host requirements for GraphPermissionMixin."""
