"""Adapter from tooling UI events to the presentation event bus."""

from __future__ import annotations

from voidx.tooling.domain import ui_events as tooling_events
from voidx.presentation.output.events import schema
from voidx.presentation.output.events import ui_events


class PresentationToolUiEventPublisher:
    @property
    def is_running(self) -> bool:
        return ui_events.is_running

    def emit(self, event: tooling_events.ToolUiEvent) -> None:
        event_type = type(event).__name__
        target = getattr(schema, event_type)
        ui_events.emit_direct(target.model_validate(event.model_dump(mode="python")))
