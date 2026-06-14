"""Host boundary for slash command handlers."""

from __future__ import annotations

import asyncio
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, Protocol

from voidx.agent.runtime_context import ContextCompilerCache, InteractionMode
from voidx.agent.task_state import TaskState
from voidx.config import Config, Settings
from voidx.llm.usage import UsageStats
from voidx.memory.service import SessionInfo
from voidx.permission.service import PermissionService

if TYPE_CHECKING:
    from voidx.lsp.manager import LspManager
    from voidx.mcp.manager import McpManager


class SlashCommandHost(Protocol):
    config: Config
    api_key: str | None
    model: Any | None

    @property
    def app(self) -> Any | None: ...
    @property
    def permission(self) -> PermissionService | None: ...
    @property
    def session(self) -> SessionInfo | None: ...
    @property
    def settings(self) -> Settings | None: ...
    @property
    @property
    def task_state(self) -> TaskState | None: ...
    @property
    def usage_stats(self) -> UsageStats | None: ...
    @property
    def workspace(self) -> str: ...
    @property
    def mcp_manager(self) -> McpManager | None: ...
    @property
    def lsp_manager(self) -> LspManager | None: ...

    def set_debug(self, value: bool) -> None: ...
    def set_interaction_mode(self, mode: str | InteractionMode) -> InteractionMode: ...
    def interaction_mode_value(self) -> str: ...
    def debug_enabled(self) -> bool: ...
    def invalidate_skill_service_cache(self) -> None: ...
    def set_task_state(self, task_state: TaskState) -> None: ...
    def can_submit_guidance(self) -> bool: ...
    def submit_guidance(self, text: str) -> bool: ...
    async def clear_current_session(self) -> bool: ...
    async def compact_session_history(self, *, force: bool = True) -> bool: ...
    async def persist_runtime_state(self) -> None: ...
    async def regenerate_session_title(self) -> bool: ...
    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool: ...
    async def resume_session(self, session: SessionInfo) -> bool: ...
    async def run_synthetic_turn(self, text: str, *, display_text: str | None = None) -> bool: ...
    async def set_session_title(self, title: str) -> bool: ...
    async def show_startup(
        self,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> bool: ...


class SlashHostAdapter:
    """Adapter that keeps slash commands behind a public host boundary.

    Tests and older integration points still construct small graph-like objects
    with private attributes. The adapter centralizes that compatibility layer so
    command modules do not each need to know both shapes.
    """

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    @property
    def config(self) -> Config | None:
        return getattr(self.raw, "config", None)

    @property
    def api_key(self) -> str | None:
        return getattr(self.raw, "api_key", None)

    @api_key.setter
    def api_key(self, value: str | None) -> None:
        setattr(self.raw, "api_key", value)

    @property
    def model(self) -> Any | None:
        return getattr(self.raw, "model", None)

    @model.setter
    def model(self, value: Any | None) -> None:
        setattr(self.raw, "model", value)

    @property
    def app(self) -> Any | None:
        return self._value("app", "_app")

    @property
    def permission(self) -> PermissionService | None:
        return self._value("permission", "_permission")

    @property
    def session(self) -> SessionInfo | None:
        return self._value("session", "_session")

    @property
    def settings(self) -> Settings | None:
        return self._value("settings", "_settings")

    @property
    def task_state(self) -> TaskState | None:
        return self._value("task_state", "_task_state")

    @property
    def usage_stats(self) -> UsageStats | None:
        return self._value("usage_stats", "_usage_stats")

    @property
    def workspace(self) -> str:
        return self._value("workspace", "_workspace", ".")

    @property
    def mcp_manager(self) -> McpManager | None:
        return self._value("mcp_manager", "_mcp_manager")

    @property
    def lsp_manager(self) -> LspManager | None:
        return self._value("lsp_manager", "_lsp_manager")

    def _legacy_attr(self, name: str, default: Any = None) -> Any:
        return getattr(self.raw, name, default)

    def _set_legacy_attr(self, name: str, value: Any) -> None:
        setattr(self.raw, name, value)

    def _value(self, public_name: str, legacy_name: str, default: Any = None) -> Any:
        try:
            return getattr(self.raw, public_name)
        except AttributeError:
            return self._legacy_attr(legacy_name, default)

    def _method(self, public_name: str, legacy_name: str | None = None) -> Any | None:
        method = getattr(self.raw, public_name, None)
        if callable(method):
            return method
        if legacy_name is not None:
            method = self._legacy_attr(legacy_name)
            if callable(method):
                return method
        return None

    async def _call_optional(
        self,
        public_name: str,
        legacy_name: str | None = None,
        *args,
        **kwargs,
    ) -> tuple[bool, Any]:
        method = self._method(public_name, legacy_name)
        if method is None:
            return False, None
        result = method(*args, **kwargs)
        if isawaitable(result):
            result = await result
        return True, result

    def set_debug(self, value: bool) -> None:
        method = self._method("set_debug")
        if method is not None:
            method(value)
            return
        self._set_legacy_attr("_debug", value)

    def debug_enabled(self) -> bool:
        method = self._method("debug_enabled")
        if method is not None:
            return bool(method())
        return bool(self._legacy_attr("_debug", False))

    def invalidate_skill_service_cache(self) -> None:
        method = self._method("invalidate_skill_service_cache", "_invalidate_skill_service_cache")
        if method is not None:
            method()

    def set_interaction_mode(self, mode: str | InteractionMode) -> InteractionMode:
        parsed = InteractionMode.parse(mode)
        method = self._method("set_interaction_mode")
        if method is not None:
            return method(parsed)
        self._set_legacy_attr("_plan_mode", parsed == InteractionMode.PLAN)
        self._set_legacy_attr("_interaction_mode", parsed)
        return parsed

    def interaction_mode_value(self) -> str:
        method = self._method("interaction_mode")
        if method is not None:
            mode = method()
        else:
            mode = self._legacy_attr("_interaction_mode")
        current = getattr(mode, "value", None)
        if current is None:
            current = "plan" if self._legacy_attr("_plan_mode", False) else "auto"
        return current

    def set_task_state(self, task_state: TaskState) -> None:
        method = self._method("set_task_state")
        if method is not None:
            method(task_state)
            return
        self._set_legacy_attr("_task_state", task_state)

    def submit_guidance(self, text: str) -> bool:
        method = self._method("submit_guidance")
        return bool(method(text)) if method is not None else False

    def can_submit_guidance(self) -> bool:
        return self._method("submit_guidance") is not None

    async def clear_current_session(self) -> bool:
        called, _ = await self._call_optional("clear_current_session")
        if called:
            return True
        await self._clear_current_session_compat()
        return True

    async def compact_session_history(self, *, force: bool = True) -> bool:
        _called, result = await self._call_optional(
            "compact_session_history",
            "_compact_session_history",
            force=force,
        )
        return bool(result)

    async def persist_runtime_state(self) -> None:
        await self._call_optional("persist_runtime_state", "_persist_runtime_state")

    async def regenerate_session_title(self) -> bool:
        _called, result = await self._call_optional("regenerate_session_title")
        return bool(result)

    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        _called, result = await self._call_optional(
            "restore_transcript_snapshot",
            "_restore_transcript_snapshot",
            append=append,
        )
        return bool(result)

    async def resume_session(self, session: SessionInfo) -> bool:
        called, _ = await self._call_optional("resume_session", None, session)
        if called:
            return True
        await self._resume_session_compat(session)
        return True

    async def run_synthetic_turn(self, text: str, *, display_text: str | None = None) -> bool:
        called, _ = await self._call_optional(
            "run_synthetic_turn",
            None,
            text,
            display_text=display_text,
        )
        return called

    async def set_session_title(self, title: str) -> bool:
        called, _ = await self._call_optional("set_session_title", None, title)
        if called:
            return True
        return await self._set_session_title_compat(title)

    async def show_startup(self, **kwargs) -> bool:
        called, _ = await self._call_optional("show_startup", "_show_startup", **kwargs)
        return called

    def _invalidate_session_title_generation(self) -> None:
        invalidator = self._method("_invalidate_session_title_generation")
        if invalidator is not None:
            invalidator()

    async def _clear_current_session_compat(self) -> None:
        session = self.session
        old_session_id = session.id if session is not None else None
        self._invalidate_session_title_generation()
        self._set_legacy_attr("_session", None)
        self._set_legacy_attr("_session_msg_cache", [])
        self._set_legacy_attr("_context_cache", ContextCompilerCache())

        reset_runtime_state = self._method("_reset_runtime_state_memory")
        if reset_runtime_state is not None:
            reset_runtime_state()
        else:
            self._set_legacy_attr("_interaction_mode", InteractionMode.AUTO)
            self._set_legacy_attr("_task_state", TaskState())
            self._set_legacy_attr("_compaction_summary", "")
            self._set_legacy_attr("_pending_summary", None)

        self._set_legacy_attr("_current_messages", None)
        pending_guidance = self._legacy_attr("_pending_guidance")
        if pending_guidance is not None:
            pending_guidance.clear()
        tracker = self._legacy_attr("_tracker")
        if tracker is not None:
            tracker.clear_todos()
        permission = self.permission
        if permission is not None:
            permission.clear_session_permissions()
        stats = self.usage_stats
        if stats is not None:
            stats.reset()
        if old_session_id:
            task = asyncio.create_task(self._clear_session_storage_compat(old_session_id))
            tasks = self._legacy_attr("_clear_session_tasks")
            if tasks is None:
                tasks = set()
                self._set_legacy_attr("_clear_session_tasks", tasks)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _clear_session_storage_compat(self, session_id: str) -> None:
        from voidx.memory.service import clear_messages, update_title
        from voidx.runtime.ui import ui

        try:
            await clear_messages(session_id)
            await update_title(session_id, "New session", touch=False)
        except Exception as exc:
            ui.print(f"[red]Clear cleanup failed: {exc}[/red]")

    async def _resume_session_compat(self, session: SessionInfo) -> None:
        self._invalidate_session_title_generation()
        self._set_legacy_attr("_session", session)
        self._set_legacy_attr("_workspace", session.workspace)
        if self.config is not None:
            self.config.workspace = session.workspace
        self._set_legacy_attr("_session_msg_cache", None)
        restore_runtime_state = self._method("_restore_runtime_state")
        if restore_runtime_state is not None:
            result = restore_runtime_state()
            if isawaitable(result):
                await result

    async def _set_session_title_compat(self, title: str) -> bool:
        session = self.session
        if session is None:
            return False

        from voidx.memory.service import update_title

        self._invalidate_session_title_generation()
        await update_title(session.id, title)
        self._set_legacy_attr("_session", session.model_copy(update={"title": title}))
        return True
