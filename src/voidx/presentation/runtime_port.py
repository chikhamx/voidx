"""Instance-level presentation implementation of the Agent UI port."""

from __future__ import annotations

import inspect
from typing import Any

from voidx.agent.ports.ui import (
    AgentConsole,
    AgentDock,
    AgentEventBus,
    AgentOutputSink,
    AgentSessionTracker,
    FrontendInteractionPort,
    FrontendStatusPort,
)


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
