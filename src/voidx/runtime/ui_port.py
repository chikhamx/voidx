"""Agent-facing UI port backed by the current runtime UI implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from voidx.runtime.ui import (
    AgentUiSink,
    console,
    dock,
    get_dock,
    session_tracker,
    show_startup,
    ui,
    ui_events,
    via_events,
)

if TYPE_CHECKING:
    from voidx.ui.output.events.schema import UiEvent
    from voidx.ui.output.tree import OutputNode, OutputTree


class AgentConsole(Protocol):
    width: int

    def print(self, *args: Any, **kwargs: Any) -> None: ...
    def __enter__(self) -> Any: ...
    def __exit__(self, *args: Any) -> Any: ...


class AgentOutputSink(AgentUiSink, Protocol):
    _TOOL_GERUND: dict[str, str]


class AgentDock(Protocol):
    active: bool
    tree: "OutputTree"
    current_agent: "OutputNode | None"

    def begin_capture(self) -> None: ...
    def deactivate(self) -> None: ...
    def reset(self) -> None: ...
    def start_turn(self, text: str) -> "OutputNode": ...
    def start_tool(
        self,
        label: str,
        args_text: str,
        *,
        tool_call_id: str = "",
        tool_name: str = "",
        raw_args: dict[str, Any] | None = None,
    ) -> "OutputNode | None": ...
    def finish_tool_node(
        self,
        node: "OutputNode | None",
        label: str,
        elapsed: float,
        ok: bool = True,
    ) -> None: ...
    def append_message(
        self,
        text: str,
        *,
        style: str = "",
        parent: "OutputNode | None" = None,
        markup: bool = False,
    ) -> "OutputNode | None": ...
    def append_file_change(
        self,
        diff_text: str,
        *,
        parent: "OutputNode | None" = None,
        tool_call_id: str = "",
    ) -> "OutputNode | None": ...
    def append_tool_result(
        self,
        text: str,
        *,
        parent: "OutputNode | None" = None,
        tool_call_id: str = "",
    ) -> "OutputNode | None": ...
    def commit_todo_state(self) -> "OutputNode | None": ...
    def clear_todo_state(self) -> None: ...
    def set_input(self, text: str, hints: list[tuple[str, str, bool]]) -> None: ...


class AgentEventBus(Protocol):
    is_running: bool

    def start(self, consumer: Any) -> None: ...
    async def emit(self, event: "UiEvent") -> bool: ...
    def emit_direct(self, event: "UiEvent") -> bool: ...
    async def request(self, event: "UiEvent") -> Any: ...
    async def drain(self) -> None: ...
    async def stop(self) -> None: ...


class AgentSessionTracker(Protocol):
    has_rollbackable_changes: bool

    def begin_turn(self, workspace: str) -> None: ...
    def finish_turn(self) -> None: ...
    def clear(self) -> None: ...
    def capture_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        workspace: str,
        extra_paths: list[str] | None = None,
    ) -> None: ...
    def record_diff(self, diff_text: str) -> None: ...
    def change_summary_lines(self) -> list[str]: ...
    def rollback_summary_lines(self) -> list[str]: ...
    def rollback_current(self) -> Any: ...


class AgentUiPort(Protocol):
    @property
    def console(self) -> AgentConsole: ...
    @property
    def ui(self) -> AgentOutputSink: ...
    @property
    def dock(self) -> AgentDock: ...
    @property
    def events(self) -> AgentEventBus: ...
    @property
    def session_tracker(self) -> AgentSessionTracker: ...

    def via_events(self) -> bool: ...
    def get_dock(self) -> AgentDock | None: ...
    def show_startup(self, **kwargs: Any) -> None: ...


class RuntimeUiPort:
    @property
    def console(self) -> AgentConsole:
        return console

    @property
    def ui(self) -> AgentOutputSink:
        return ui

    @property
    def dock(self) -> AgentDock:
        return dock

    @property
    def events(self) -> AgentEventBus:
        return ui_events

    @property
    def session_tracker(self) -> AgentSessionTracker:
        return session_tracker

    def via_events(self) -> bool:
        return via_events()

    def get_dock(self) -> AgentDock | None:
        return get_dock()

    def show_startup(self, **kwargs: Any) -> None:
        show_startup(**kwargs)


runtime_ui_port = RuntimeUiPort()


__all__ = [
    "AgentConsole",
    "AgentDock",
    "AgentEventBus",
    "AgentOutputSink",
    "AgentSessionTracker",
    "AgentUiPort",
    "RuntimeUiPort",
    "runtime_ui_port",
]
