"""Typed UI events public API."""

from __future__ import annotations

from voidx.ui.output.dock import dock
from voidx.ui.output.events.bus import UiEventBus
from voidx.ui.output.events.consumers import CompositeEventConsumer, DockEventConsumer
from voidx.ui.output.events.schema import (
    AnsiAppended,
    AssistantStreamCommitted,
    AssistantStreamDiscarded,
    AssistantStreamStarted,
    AssistantStreamUpdated,
    CaptureStarted,
    CaptureStopped,
    DiffAppended,
    ErrorAppended,
    FileChangeAppended,
    GuidanceSubmitted,
    InputSet,
    MarkdownAppended,
    MessageAppended,
    NoticeSet,
    PermissionPromptCleared,
    PermissionPromptShown,
    PermissionToolDetail,
    RefreshRequested,
    ResetRequested,
    StartupShown,
    StatusFinished,
    StatusUpdated,
    SubagentFinished,
    SubagentStarted,
    SubagentStepStarted,
    ThoughtAppended,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    TodoCleared,
    TodoCommitted,
    TodoItemPayload,
    TodoUpdated,
    TurnStarted,
    UiEvent,
    UiEventBase,
    WarningAppended,
)


ui_events = UiEventBus()


def via_events() -> bool:
    """True when the dock is active and the UI event bus is the preferred rendering path."""
    return dock.active and ui_events.is_running


__all__ = [
    "AnsiAppended",
    "AssistantStreamCommitted",
    "AssistantStreamDiscarded",
    "AssistantStreamStarted",
    "AssistantStreamUpdated",
    "CaptureStarted",
    "CaptureStopped",
    "CompositeEventConsumer",
    "DiffAppended",
    "DockEventConsumer",
    "ErrorAppended",
    "FileChangeAppended",
    "GuidanceSubmitted",
    "InputSet",
    "MarkdownAppended",
    "MessageAppended",
    "NoticeSet",
    "PermissionPromptCleared",
    "PermissionPromptShown",
    "PermissionToolDetail",
    "RefreshRequested",
    "ResetRequested",
    "StartupShown",
    "StatusFinished",
    "StatusUpdated",
    "SubagentFinished",
    "SubagentStarted",
    "SubagentStepStarted",
    "ThoughtAppended",
    "ToolFinished",
    "ToolResultAppended",
    "ToolStarted",
    "TodoCleared",
    "TodoCommitted",
    "TodoItemPayload",
    "TodoUpdated",
    "TurnStarted",
    "UiEvent",
    "UiEventBase",
    "UiEventBus",
    "WarningAppended",
    "ui_events",
    "via_events",
]
