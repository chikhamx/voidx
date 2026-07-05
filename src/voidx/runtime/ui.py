"""Lazy UI boundary used by the agent runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Protocol


class AgentUiSink(Protocol):
    width: int
    console: Any

    def set_debug(self, value: bool) -> None: ...
    def print(self, *args: Any, **kwargs: Any) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def step_header(self, agent: str = "") -> None: ...
    def tool_call(self, tool_name: str, args: dict[str, object]) -> None: ...
    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None: ...
    def tool_result(self, text: str) -> None: ...
    def diff(self, diff_text: str) -> None: ...


class _NoOpConsole:
    width = 80

    def __enter__(self) -> "_NoOpConsole":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def print(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def noop(*args: Any, **kwargs: Any) -> None:
            return None

        return noop


class NoOpAgentUiSink:
    width = 80

    def __init__(self) -> None:
        self.console = _NoOpConsole()

    def set_debug(self, value: bool) -> None:
        return None

    def print(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warn(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None

    def step_header(self, agent: str = "") -> None:
        return None

    def tool_call(self, tool_name: str, args: dict[str, object]) -> None:
        return None

    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None:
        return None

    def tool_result(self, text: str) -> None:
        return None

    def diff(self, diff_text: str) -> None:
        return None


class _LazyAttr:
    def __init__(self, module_name: str, attr_name: str) -> None:
        self._module_name = module_name
        self._attr_name = attr_name
        self._cached: Any = None

    def _target(self) -> Any:
        if self._cached is None:
            self._cached = getattr(import_module(self._module_name), self._attr_name)
        return self._cached

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._target()(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __iter__(self):
        return iter(self._target())

    def __len__(self) -> int:
        return len(self._target())

    def __getitem__(self, key: Any) -> Any:
        return self._target()[key]

    def __bool__(self) -> bool:
        return bool(self._target())


class _LazyConsole:
    _target: AgentUiSink | None = None

    def set_target(self, target: AgentUiSink | None) -> None:
        self._target = target

    def _load(self) -> AgentUiSink:
        if self._target is None:
            cls = getattr(import_module("voidx.ui.output.console"), "VoidConsole")
            self._target = cls()
        return self._target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


class _ConsoleProxy:
    def _target(self) -> Any:
        return ui.console

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __enter__(self) -> Any:
        target = self._target()
        enter = getattr(type(target), "__enter__", None)
        if enter is None:
            return target
        return enter(target)

    def __exit__(self, *args: Any) -> Any:
        target = self._target()
        exit_ = getattr(type(target), "__exit__", None)
        if exit_ is None:
            return None
        return exit_(target, *args)


_ui_proxy = _LazyConsole()
ui: AgentUiSink = _ui_proxy  # type: ignore[assignment]
console = _ConsoleProxy()


def set_ui_sink(sink: AgentUiSink) -> None:
    _ui_proxy.set_target(sink)


def use_noop_ui_sink() -> NoOpAgentUiSink:
    sink = NoOpAgentUiSink()
    set_ui_sink(sink)
    return sink


def reset_ui_sink() -> None:
    _ui_proxy.set_target(None)


COMMANDS = _LazyAttr("voidx.ui.commands", "COMMANDS")
AssistantStreamCommitted = _LazyAttr("voidx.ui.output.events", "AssistantStreamCommitted")
AssistantStreamUpdated = _LazyAttr("voidx.ui.output.events", "AssistantStreamUpdated")
CaptureConsole = _LazyAttr("voidx.ui.output.capture", "CaptureConsole")
CompositeEventConsumer = _LazyAttr("voidx.ui.output.events", "CompositeEventConsumer")
DockEventConsumer = _LazyAttr("voidx.ui.output.events", "DockEventConsumer")
FileChangeAppended = _LazyAttr("voidx.ui.output.events", "FileChangeAppended")
GatewayEventConsumer = _LazyAttr("voidx.ui.gateway", "GatewayEventConsumer")
GatewayServer = _LazyAttr("voidx.ui.gateway", "GatewayServer")
GatewaySession = _LazyAttr("voidx.ui.gateway", "GatewaySession")
GuidanceSubmitted = _LazyAttr("voidx.ui.output.events", "GuidanceSubmitted")
InputSet = _LazyAttr("voidx.ui.output.events", "InputSet")
McpServerStatus = _LazyAttr("voidx.ui.output.types", "McpServerStatus")
ThreadExecutionContext = _LazyAttr("voidx.ui.output.types", "ThreadExecutionContext")
OutputNode = _LazyAttr("voidx.ui.output.tree", "OutputNode")
OutputTree = _LazyAttr("voidx.ui.output.tree", "OutputTree")
PermissionToolDetail = _LazyAttr("voidx.ui.output.events", "PermissionToolDetail")
PermissionPromptShown = _LazyAttr("voidx.ui.output.events", "PermissionPromptShown")
PermissionPromptCleared = _LazyAttr("voidx.ui.output.events", "PermissionPromptCleared")
PureTui = _LazyAttr("voidx.ui.tui", "PureTui")
StartupShown = _LazyAttr("voidx.ui.output.events", "StartupShown")
StatusFinished = _LazyAttr("voidx.ui.output.events", "StatusFinished")
StatusUpdated = _LazyAttr("voidx.ui.output.events", "StatusUpdated")
StreamingRenderer = _LazyAttr("voidx.ui.output.console", "StreamingRenderer")
SubagentFinished = _LazyAttr("voidx.ui.output.events", "SubagentFinished")
SubagentStarted = _LazyAttr("voidx.ui.output.events", "SubagentStarted")
ToolFinished = _LazyAttr("voidx.ui.output.events", "ToolFinished")
ToolResultAppended = _LazyAttr("voidx.ui.output.events", "ToolResultAppended")
ToolStarted = _LazyAttr("voidx.ui.output.events", "ToolStarted")
TodoCleared = _LazyAttr("voidx.ui.output.events", "TodoCleared")
TodoCommitted = _LazyAttr("voidx.ui.output.events", "TodoCommitted")
TodoItemPayload = _LazyAttr("voidx.ui.output.events", "TodoItemPayload")
TodoUpdated = _LazyAttr("voidx.ui.output.events", "TodoUpdated")
TurnStarted = _LazyAttr("voidx.ui.output.events", "TurnStarted")
UiStatus = _LazyAttr("voidx.ui.output.types", "UiStatus")
WarningAppended = _LazyAttr("voidx.ui.output.events", "WarningAppended")
ToolDisplayMode = _LazyAttr("voidx.ui.output.display_policy", "ToolDisplayMode")
ToolDisplayPolicy = _LazyAttr("voidx.ui.output.display_policy", "ToolDisplayPolicy")
DEFAULT_DISPLAY_RULES = _LazyAttr("voidx.ui.output.display_policy", "DEFAULT_DISPLAY_RULES")
dock = _LazyAttr("voidx.ui.output.dock", "dock")
session_tracker = _LazyAttr("voidx.ui.session", "session_tracker")
ui_events = _LazyAttr("voidx.ui.output.events", "ui_events")


def _attr(module_name: str, attr_name: str) -> Any:
    return getattr(import_module(module_name), attr_name)


def _fmt_args(args: dict[str, object]) -> str:
    return _attr("voidx.ui.output.console", "_fmt_args")(args)


def _title(text: str) -> str:
    return _attr("voidx.ui.output.console", "_title")(text)


def code_ide_status(settings: Any) -> str:
    return _attr("voidx.ui.tools.code_ide", "code_ide_status")(settings)


def detect_code_ides() -> list[Any]:
    return _attr("voidx.ui.tools.code_ide", "detect_code_ides")()


def emit_web_gateway_bootstrap(url: str) -> None:
    return _attr("voidx.ui.gateway.bootstrap", "emit_web_gateway_bootstrap")(url)


def get_dock() -> Any:
    return _attr("voidx.ui.output.dock", "get_dock")()


def normalize_ide(value: str) -> str:
    return _attr("voidx.ui.tools.code_ide", "normalize_ide")(value)


def show_startup(**kwargs: Any) -> None:
    return _attr("voidx.ui.session", "show_startup")(**kwargs)


def transcript_rows_to_tree(rows: list[Any]) -> Any:
    return _attr("voidx.ui.transcript", "transcript_rows_to_tree")(rows)


def tree_to_transcript_rows(session_id: str, tree: Any) -> tuple[list[Any], int]:
    return _attr("voidx.ui.transcript", "tree_to_transcript_rows")(session_id, tree)


def ui_command_kind(command: Any) -> str:
    return str(getattr(command, "kind", "") or "")


def via_events() -> bool:
    return bool(_attr("voidx.ui.output.events", "via_events")())
