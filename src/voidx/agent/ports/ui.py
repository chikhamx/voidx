"""Agent-facing UI output port."""

from __future__ import annotations

from typing import Any, Protocol


class UiEventTimeout(TimeoutError):
    pass


class FrontendInteractionPort(Protocol):
    async def ask_choice(self, prompt: str, choices: list[Any], **kwargs: Any) -> str | None: ...
    async def ask_text(self, prompt: str, **kwargs: Any) -> str | None: ...


class FrontendStatusPort(Protocol):
    status: Any

    def invalidate_skill_service_cache(self) -> None: ...
    def invalidate(self) -> None: ...


class InteractionFrontend(FrontendInteractionPort, FrontendStatusPort, Protocol):
    pass


class AgentConsole(Protocol):
    width: int

    def print(self, *args: Any, **kwargs: Any) -> None: ...
    def __enter__(self) -> Any: ...
    def __exit__(self, *args: Any) -> Any: ...


class AgentOutputSink(Protocol):
    width: int
    console: AgentConsole
    _TOOL_GERUND: dict[str, str]

    def set_debug(self, value: bool) -> None: ...
    def print(self, *args: Any, **kwargs: Any) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def step_header(self, agent: str = "") -> None: ...
    def tool_call(self, tool_name: str, args: dict[str, object]) -> None: ...
    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None: ...
    def tool_result(self, text: str) -> None: ...
    def diff(self, diff_text: str) -> None: ...


class AgentDock(Protocol):
    active: bool
    tree: Any
    current_agent: Any

    def begin_capture(self) -> None: ...
    def deactivate(self) -> None: ...
    def reset(self) -> None: ...
    def start_turn(self, text: str) -> Any: ...
    def start_tool(
        self,
        label: str,
        args_text: str,
        *,
        tool_call_id: str = "",
        tool_name: str = "",
        raw_args: dict[str, Any] | None = None,
    ) -> Any: ...
    def finish_tool_node(
        self,
        node: Any,
        label: str,
        elapsed: float,
        ok: bool = True,
    ) -> None: ...
    def append_message(
        self,
        text: str,
        *,
        style: str = "",
        parent: Any = None,
        markup: bool = False,
    ) -> Any: ...
    def append_file_change(
        self,
        diff_text: str,
        *,
        parent: Any = None,
        tool_call_id: str = "",
    ) -> Any: ...
    def append_tool_result(
        self,
        text: str,
        *,
        parent: Any = None,
        tool_call_id: str = "",
    ) -> Any: ...
    def commit_todo_state(self) -> Any: ...
    def clear_todo_state(self) -> None: ...
    def set_input(self, text: str, hints: list[tuple[str, str, bool]]) -> None: ...


class AgentEventBus(Protocol):
    is_running: bool

    def start(self, consumer: Any) -> None: ...
    async def emit(self, event: Any) -> bool: ...
    def emit_direct(self, event: Any) -> bool: ...
    async def request(self, event: Any) -> Any: ...
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

    def bind_frontend(self, frontend: InteractionFrontend | None) -> None: ...
    async def ask_choice(self, prompt: str, choices: list[Any], **kwargs: Any) -> str | None: ...
    async def ask_text(self, prompt: str, **kwargs: Any) -> str | None: ...
    def invalidate_skill_service_cache(self) -> None: ...
    def update_status(self, **values: Any) -> None: ...
    def invalidate(self) -> None: ...
    def streaming_renderer(self, console: Any, **kwargs: Any) -> Any: ...
    def capture_console(self, tree: Any, parent: Any, *, agent_id: int = -1) -> Any: ...
    def output_tree(self) -> Any: ...
    def format_args(self, args: dict[str, Any]) -> str: ...
    def title(self, tool_name: str) -> str: ...



class _NullConsole:
    width = 80

    def print(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None



class _NullRenderer:
    def __init__(self, *, headless: bool = False, **kwargs: Any) -> None:
        self._headless = headless
        self._stream_to_dock = bool(kwargs.get("stream_to_dock", True))

    def start(self) -> None:
        return None

    def update(self, *args: Any, **kwargs: Any) -> None:
        return None

    def done(self) -> None:
        return None


class _NullEvents:
    is_running = False

    def start(self, consumer: Any) -> None:
        return None

    async def emit(self, event: Any) -> bool:
        return False

    def emit_direct(self, event: Any) -> bool:
        return False

    async def request(self, event: Any) -> Any:
        return None

    async def drain(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class NullAgentUiPort:
    def __init__(self) -> None:
        self._console = _NullConsole()
        self._events = _NullEvents()

    @property
    def console(self) -> Any:
        return self._console

    @property
    def ui(self) -> Any:
        return self._console

    @property
    def dock(self) -> Any:
        return self._console

    @property
    def events(self) -> Any:
        return self._events

    @property
    def session_tracker(self) -> Any:
        return self._console

    def via_events(self) -> bool:
        return False

    def get_dock(self) -> None:
        return None

    def show_startup(self, **kwargs: Any) -> None:
        return None

    def bind_frontend(self, frontend: InteractionFrontend | None) -> None:
        return None

    async def ask_choice(self, prompt: str, choices: list[Any], **kwargs: Any) -> str | None:
        return None

    async def ask_text(self, prompt: str, **kwargs: Any) -> str | None:
        return None

    def invalidate_skill_service_cache(self) -> None:
        return None

    def update_status(self, **values: Any) -> None:
        return None

    def invalidate(self) -> None:
        return None

    def streaming_renderer(self, console: Any, **kwargs: Any) -> Any:
        return _NullRenderer(**kwargs)

    def capture_console(self, tree: Any, parent: Any, *, agent_id: int = -1) -> Any:
        return self._console

    def output_tree(self) -> Any:
        return None

    def format_args(self, args: dict[str, Any]) -> str:
        return str(args)

    def title(self, tool_name: str) -> str:
        return tool_name
