import asyncio
import logging
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text


from voidx.ui.output.capture import CaptureConsole
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.output.display_policy import ToolDisplayMode
from voidx.ui.output.events import (
    AssistantStreamCommitted,
    AssistantStreamUpdated,
    CheckpointChoicePayload,
    CheckpointDecisionSubmitted,
    CheckpointPlanPayload,
    CheckpointPromptShown,
    ClarifyAnswerSubmitted,
    ClarifyPromptShown,
    DockEventConsumer,
    ErrorAppended,
    FileChangeAppended,
    GuidanceSubmitted,
    PermissionPromptCleared,
    PermissionPromptShown,
    PermissionToolDetail,
    StartupShown,
    StatusFinished,
    StatusUpdated,
    SubagentFinished,
    SubagentStarted,
    SubagentStepStarted,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    TodoCleared,
    TodoCommitted,
    TodoItemPayload,
    TodoUpdated,
    TurnStarted,
    UiEventBus,
    ui_events,
)
from voidx.ui.output.tree import OutputTree

from tests.test_ui.gateway.conftest import _plain, _rich_plain, _tree_nodes, isolated_dock

def test_dock_event_consumer_rejects_unsupported_event(isolated_dock):
    consumer = DockEventConsumer(isolated_dock)

    with pytest.raises(TypeError, match="Unsupported UI event"):
        consumer.handle(object())


def test_streaming_renderer_done_refreshes_direct_dock_once(isolated_dock, monkeypatch):
    isolated_dock.begin_capture()
    refreshes = 0

    def counted_refresh():
        nonlocal refreshes
        refreshes += 1

    monkeypatch.setattr(isolated_dock, "refresh", counted_refresh)
    renderer = StreamingRenderer(Console(), debug=False)
    renderer.feed_text("hello")
    refreshes = 0

    renderer.done()

    assert refreshes == 1


@pytest.mark.asyncio
async def test_ui_event_bus_exposes_consumer_error_on_drain(isolated_dock):
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(object())

        with pytest.raises(TypeError, match="Unsupported UI event"):
            await bus.drain()
        assert isinstance(bus.last_error, TypeError)
    finally:
        await bus.stop()


@pytest.fixture(autouse=True)
def isolated_dock():
    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        yield test_dock
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_ui_event_bus_serializes_tool_updates_by_call_id(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))

        async def run_tool(call_id: str, label: str, result: str) -> None:
            await bus.request(ToolStarted(agent_id=-1, tool_call_id=call_id, label=label, args='file_path="x"'))
            await asyncio.sleep(0)
            await bus.emit(ToolFinished(agent_id=-1, tool_call_id=call_id, label=label, elapsed=0.1, ok=True))
            await bus.emit(ToolResultAppended(agent_id=-1, tool_call_id=call_id, text=result, collapsed=False))

        await asyncio.gather(
            run_tool("call_1", "Reading", "first result"),
            run_tool("call_2", "Mapping", "second result"),
        )
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        tools = {node.header: node for node in assistant.children if node.node_type == "tool_call"}

        visible_headers = "\n".join(_rich_plain(header) for header in tools)
        reading = next(node for header, node in tools.items() if 'Read("x")' in _rich_plain(header))
        mapping = next(node for header, node in tools.items() if 'Map' in _rich_plain(header))
        assert "[cyan]" not in visible_headers
        assert reading.children[0].header == "first result"
        assert mapping.children[0].header == "second result"
    finally:
        await bus.stop()




@pytest.mark.asyncio
async def test_ui_event_bus_request_times_out_without_cancelling_consumer_future():
    from voidx.ui.output.events.bus import UiEventTimeout

    class SlowConsumer:
        async def handle(self, event):
            await asyncio.sleep(0.2)
            return "eventually-done"

    bus = UiEventBus()
    bus.start(SlowConsumer())
    try:
        with pytest.raises(UiEventTimeout, match="timed out"):
            await bus.request(TurnStarted(text="slow"), timeout=0.01, max_retries=2)

        await asyncio.sleep(0.25)
        assert bus.last_error is None
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_request_returns_before_timeout_when_consumer_finishes():
    class FastConsumer:
        async def handle(self, event):
            await asyncio.sleep(0)
            return "ok"

    bus = UiEventBus()
    bus.start(FastConsumer())
    try:
        assert await bus.request(TurnStarted(text="fast"), timeout=0.05, max_retries=2) == "ok"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_request_timeout_writes_tool_log(monkeypatch):
    from voidx.ui.output.events import bus as bus_module
    from voidx.ui.output.events.bus import UiEventTimeout

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bus_module,
        "log_tool_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    class SlowConsumer:
        async def handle(self, event):
            await asyncio.sleep(0.2)
            return "eventually-done"

    bus = UiEventBus()
    bus.start(SlowConsumer())
    try:
        with pytest.raises(UiEventTimeout):
            await bus.request(TurnStarted(text="slow"), timeout=0.01, max_retries=2)
        await asyncio.sleep(0.25)
    finally:
        await bus.stop()

    event_names = [event for event, _kwargs in events]
    assert "ui_event_bus_request_stall" in event_names
    assert "ui_event_bus_request_timeout" in event_names
    timeout = next(kwargs for event, kwargs in events if event == "ui_event_bus_request_timeout")
    assert timeout["tool_name"] == "ui_event_bus"
    assert "timed out" in timeout["message"]
