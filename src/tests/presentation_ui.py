"""Explicit presentation UI adapter factory for tests."""

from voidx.presentation.output.console import VoidConsole
from voidx.presentation.output.dock import BottomInputDock, dock as current_dock
from voidx.presentation.output.events import UiEventBus, ui_events
from voidx.presentation.runtime_port import PresentationUiAdapter
from voidx.presentation.session import session_tracker


def make_presentation_ui(
    *,
    dock: BottomInputDock | None = None,
    events: UiEventBus | None = None,
) -> PresentationUiAdapter:
    return PresentationUiAdapter(
        output=VoidConsole(),
        dock=dock or current_dock,
        events=events or ui_events,
        session_tracker=session_tracker,
    )
