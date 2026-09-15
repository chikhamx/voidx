"""Instance-level presentation implementation of the Agent UI port."""

from __future__ import annotations

import inspect
from typing import Any

from typing import Protocol


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
        detail: str = "",
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
    async def prepare_session_switch(self) -> None: ...
    async def flush_after_restore(self) -> None: ...
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

    async def prepare_session_switch(self) -> None:
        return None

    async def flush_after_restore(self) -> None:
        return None


class PresentationUiAdapter:
    def __init__(
        self,
        *,
        output: AgentOutputSink,
        dock: AgentDock,
        events: AgentEventBus,
        session_tracker: AgentSessionTracker,
    ) -> None:
        self._output = output
        self._dock = dock
        self._events = events
        self._session_tracker = session_tracker
        self._interaction_frontend: FrontendInteractionPort | None = None
        self._status_frontend: FrontendStatusPort | None = None

    @property
    def console(self) -> AgentConsole:
        return self._output.console

    @property
    def ui(self) -> AgentOutputSink:
        return self._output

    @property
    def dock(self) -> AgentDock:
        return self._dock

    @property
    def events(self) -> AgentEventBus:
        return self._events

    @property
    def session_tracker(self) -> AgentSessionTracker:
        return self._session_tracker

    def via_events(self) -> bool:
        return self._dock.active and self._events.is_running

    def get_dock(self) -> AgentDock | None:
        return self._dock

    def bind_frontend(self, frontend: FrontendInteractionPort | FrontendStatusPort | None) -> None:
        self._interaction_frontend = frontend
        self._status_frontend = frontend

    async def ask_choice(self, prompt: str, choices: list[Any], **kwargs: Any) -> str | None:
        if self._interaction_frontend is None:
            return None
        try:
            parameters = inspect.signature(self._interaction_frontend.ask_choice).parameters
        except (TypeError, ValueError):
            parameters = {}
        if parameters and not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs = {key: value for key, value in kwargs.items() if key in parameters}
        return await self._interaction_frontend.ask_choice(prompt, choices, **kwargs)

    async def ask_text(self, prompt: str, **kwargs: Any) -> str | None:
        if self._interaction_frontend is None:
            return None
        return await self._interaction_frontend.ask_text(prompt, **kwargs)

    def invalidate_skill_service_cache(self) -> None:
        if self._status_frontend is not None:
            self._status_frontend.invalidate_skill_service_cache()

    def update_status(self, **values: Any) -> None:
        if self._status_frontend is None:
            return
        for name, value in values.items():
            setattr(self._status_frontend.status, name, value)

    def invalidate(self) -> None:
        if self._status_frontend is not None:
            self._status_frontend.invalidate()

    def show_startup(self, **kwargs: Any) -> None:
        from voidx.presentation.session import show_startup

        show_startup(**kwargs)

    def streaming_renderer(self, console: Any, **kwargs: Any) -> Any:
        from voidx.presentation.output.console import StreamingRenderer

        return StreamingRenderer(console, **kwargs)

    def capture_console(self, tree: Any, parent: Any, *, agent_id: int = -1) -> Any:
        from voidx.presentation.output.capture import CaptureConsole

        return CaptureConsole(tree, parent, agent_id=agent_id)

    def output_tree(self) -> Any:
        from voidx.presentation.output.tree import OutputTree

        return OutputTree()

    def format_args(self, args: dict[str, Any]) -> str:
        from voidx.presentation.output.console import format_tool_args

        return format_tool_args(args)

    def title(self, tool_name: str) -> str:
        from voidx.presentation.output.console import format_tool_title

        return format_tool_title(tool_name)

    async def prepare_session_switch(self) -> None:
        frontend = self._status_frontend
        prepare = getattr(frontend, "prepare_session_switch", None)
        if not callable(prepare):
            return
        result = prepare()
        if inspect.isawaitable(result):
            await result

    async def flush_after_restore(self) -> None:
        frontend = self._status_frontend
        flush = getattr(frontend, "flush_after_restore", None)
        if not callable(flush):
            return
        result = flush()
        if inspect.isawaitable(result):
            await result
