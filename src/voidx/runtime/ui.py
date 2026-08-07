"""Lazy UI boundary used by the agent runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Protocol



class UiEventTimeout(TimeoutError):
    """Raised when a UI event request is not handled in time."""

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
            cls = getattr(import_module("voidx.presentation.output.console"), "VoidConsole")
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



def _default_tui_frontend_factory(status: Any, commands: list[tuple[str, str]]) -> Any:
    try:
        cls = getattr(import_module("voidx_cli"), "PureTui")
    except ModuleNotFoundError:
        raise RuntimeError(
            "voidx_cli is required for terminal UI mode. "
            "Install it with: pip install voidx-cli, or reinstall via npm (npm install -g @chikhamx/voidx)"
        ) from None
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load voidx_cli: {exc}. "
            "Reinstall with: pip install voidx-cli, or reinstall via npm (npm install -g @chikhamx/voidx)"
        ) from exc
    try:
        return cls(status, commands)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to initialize terminal UI: {exc}. "
            "Reinstall with: pip install voidx-cli, or reinstall via npm (npm install -g @chikhamx/voidx)"
        ) from exc


FrontendFactory = Any
_default_frontend_factory: FrontendFactory | None = _default_tui_frontend_factory


def register_default_frontend(factory: FrontendFactory) -> None:
    global _default_frontend_factory
    _default_frontend_factory = factory


def reset_default_frontend() -> None:
    global _default_frontend_factory
    _default_frontend_factory = None


def create_frontend(status: Any, commands: list[tuple[str, str]]) -> Any:
    if _default_frontend_factory is None:
        raise RuntimeError("No frontend registered. Install or register an interaction frontend.")
    return _default_frontend_factory(status, commands)
COMMANDS = _LazyAttr("voidx.presentation.commands", "COMMANDS")
AssistantStreamCommitted = _LazyAttr("voidx.presentation.output.events", "AssistantStreamCommitted")
AssistantStreamUpdated = _LazyAttr("voidx.presentation.output.events", "AssistantStreamUpdated")
CaptureConsole = _LazyAttr("voidx.presentation.output.capture", "CaptureConsole")
CompositeEventConsumer = _LazyAttr("voidx.presentation.output.events", "CompositeEventConsumer")
DockEventConsumer = _LazyAttr("voidx.presentation.output.events", "DockEventConsumer")
FileChangeAppended = _LazyAttr("voidx.presentation.output.events", "FileChangeAppended")
GatewayEventConsumer = _LazyAttr("voidx.presentation.gateway", "GatewayEventConsumer")
GatewayHeadlessFrontend = _LazyAttr("voidx.presentation.gateway", "GatewayHeadlessFrontend")
GatewayServer = _LazyAttr("voidx.presentation.gateway", "GatewayServer")
GatewaySession = _LazyAttr("voidx.presentation.gateway", "GatewaySession")
GuidanceCommitted = _LazyAttr("voidx.presentation.output.events", "GuidanceCommitted")
GuidanceSubmitted = _LazyAttr("voidx.presentation.output.events", "GuidanceSubmitted")
MessageAppended = _LazyAttr("voidx.presentation.output.events", "MessageAppended")
InputSet = _LazyAttr("voidx.presentation.output.events", "InputSet")
McpServerStatus = _LazyAttr("voidx.presentation.output.types", "McpServerStatus")
OutputNode = _LazyAttr("voidx.presentation.output.tree", "OutputNode")
OutputTree = _LazyAttr("voidx.presentation.output.tree", "OutputTree")
PermissionToolDetail = _LazyAttr("voidx.presentation.output.events", "PermissionToolDetail")
PermissionPromptShown = _LazyAttr("voidx.presentation.output.events", "PermissionPromptShown")
PermissionPromptCleared = _LazyAttr("voidx.presentation.output.events", "PermissionPromptCleared")
StartupShown = _LazyAttr("voidx.presentation.output.events", "StartupShown")
RefreshRequested = _LazyAttr("voidx.presentation.output.events", "RefreshRequested")
StatusFinished = _LazyAttr("voidx.presentation.output.events", "StatusFinished")
StatusUpdated = _LazyAttr("voidx.presentation.output.events", "StatusUpdated")
StreamingRenderer = _LazyAttr("voidx.presentation.output.console", "StreamingRenderer")
SubagentFinished = _LazyAttr("voidx.presentation.output.events", "SubagentFinished")
SubagentStarted = _LazyAttr("voidx.presentation.output.events", "SubagentStarted")
ToolFinished = _LazyAttr("voidx.presentation.output.events", "ToolFinished")
ToolResultAppended = _LazyAttr("voidx.presentation.output.events", "ToolResultAppended")
TurnCancelled = _LazyAttr("voidx.presentation.output.events", "TurnCancelled")
TurnCompleted = _LazyAttr("voidx.presentation.output.events", "TurnCompleted")
TurnFailed = _LazyAttr("voidx.presentation.output.events", "TurnFailed")
ToolStarted = _LazyAttr("voidx.presentation.output.events", "ToolStarted")
TodoCleared = _LazyAttr("voidx.presentation.output.events", "TodoCleared")
TodoCommitted = _LazyAttr("voidx.presentation.output.events", "TodoCommitted")
TodoItemPayload = _LazyAttr("voidx.presentation.output.events", "TodoItemPayload")
TodoUpdated = _LazyAttr("voidx.presentation.output.events", "TodoUpdated")
TurnStarted = _LazyAttr("voidx.presentation.output.events", "TurnStarted")
UiStatus = _LazyAttr("voidx.presentation.output.types", "UiStatus")
WarningAppended = _LazyAttr("voidx.presentation.output.events", "WarningAppended")
ToolDisplayMode = _LazyAttr("voidx.presentation.output.display_policy", "ToolDisplayMode")
ToolDisplayPolicy = _LazyAttr("voidx.presentation.output.display_policy", "ToolDisplayPolicy")
DEFAULT_DISPLAY_RULES = _LazyAttr("voidx.presentation.output.display_policy", "DEFAULT_DISPLAY_RULES")
dock = _LazyAttr("voidx.presentation.output.dock", "dock")
session_tracker = _LazyAttr("voidx.presentation.session", "session_tracker")
ui_events = _LazyAttr("voidx.presentation.output.events", "ui_events")
InteractionFrontend = _LazyAttr("voidx.presentation.output.types", "InteractionFrontend")
paste_clipboard_image = _LazyAttr("voidx.presentation.tools.clipboard_image", "paste_clipboard_image")


def _attr(module_name: str, attr_name: str) -> Any:
    return getattr(import_module(module_name), attr_name)


def _fmt_args(args: dict[str, object]) -> str:
    return _attr("voidx.presentation.output.console", "_fmt_args")(args)


def _title(text: str) -> str:
    return _attr("voidx.presentation.output.console", "_title")(text)


def code_ide_status(settings: Any) -> str:
    return _attr("voidx.presentation.tools.code_ide", "code_ide_status")(settings)


def detect_code_ides() -> list[Any]:
    return _attr("voidx.presentation.tools.code_ide", "detect_code_ides")()


def emit_web_gateway_bootstrap(url: str) -> None:
    return _attr("voidx.presentation.gateway.bootstrap", "emit_web_gateway_bootstrap")(url)


def get_dock() -> Any:
    return _attr("voidx.presentation.output.dock", "get_dock")()


def normalize_ide(value: str) -> str:
    return _attr("voidx.presentation.tools.code_ide", "normalize_ide")(value)


def show_startup(**kwargs: Any) -> None:
    return _attr("voidx.presentation.session", "show_startup")(**kwargs)


def transcript_rows_to_tree(rows: list[Any]) -> Any:
    return _attr("voidx.presentation.transcript_snapshot", "transcript_rows_to_tree")(rows)


def tree_to_transcript_rows(session_id: str, tree: Any) -> tuple[list[Any], int]:
    return _attr("voidx.presentation.transcript_snapshot", "tree_to_transcript_rows")(session_id, tree)


def ui_command_kind(command: Any) -> str:
    return str(getattr(command, "kind", "") or "")


def via_events() -> bool:
    return bool(_attr("voidx.presentation.output.events", "via_events")())
