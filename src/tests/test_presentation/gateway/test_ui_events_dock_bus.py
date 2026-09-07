import asyncio
import logging
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text


from voidx.presentation.output.capture import CaptureConsole
from voidx.presentation.output.console import StreamingRenderer
from voidx.presentation.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.presentation.output.display_policy import ToolDisplayMode
from voidx.presentation.output.events import (
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
from voidx.presentation.output.tree import OutputTree

from tests.test_presentation.gateway.conftest import _plain, _rich_plain, _tree_nodes, isolated_dock

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
        reading_result = next(
            node for node in assistant.children
            if node.node_type == "tool_result"
            and node.tool_call_id == "call_1"
        )
        mapping_result = next(
            node for node in assistant.children
            if node.node_type == "tool_result"
            and node.tool_call_id == "call_2"
        )
        assert reading_result.parent is assistant
        assert mapping_result.parent is assistant
        assert reading_result.header == "first result"
        assert mapping_result.header == "second result"
    finally:
        await bus.stop()




@pytest.mark.asyncio
async def test_tool_completion_is_completed_but_not_writer_settled(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(ToolStarted(
            tool_call_id="tool_state",
            tool_name="read",
            label="Reading",
            args='file_path="x.py"',
        ))
        await bus.emit(ToolFinished(
            tool_call_id="tool_state",
            label="Reading",
            elapsed=0.1,
        ))
        await bus.drain()

        tool = next(
            node for node in _tree_nodes(isolated_dock.tree.root)
            if node.node_type == "tool_call"
        )
        assert tool.payload["lifecycle"] == "completed"
        assert tool.id not in isolated_dock._settled_node_ids
    finally:
        await bus.stop()


def test_completed_prefix_waits_behind_previous_running_node(isolated_dock):
    isolated_dock.begin_capture()
    isolated_dock.start_turn("demo")
    first = isolated_dock.start_tool("Reading", tool_name="read")
    second = isolated_dock.start_tool("Mapping", tool_name="search")
    isolated_dock.finish_tool_node(second, "Mapping", 0.1, True)

    lines = isolated_dock.tree.render(120)
    limit = isolated_dock.safe_flush_line_count(120, 0)

    assert first.payload["lifecycle"] == "running"
    assert second.payload["lifecycle"] == "completed"
    assert limit < len(lines)
    assert "Mapping" not in "\n".join(lines[:limit])

    isolated_dock.finish_tool_node(first, "Reading", 0.1, True)
    assert isolated_dock.safe_flush_line_count(120, 0) == len(
        isolated_dock.tree.render(120)
    )


@pytest.mark.asyncio
async def test_ui_event_bus_request_times_out_without_cancelling_consumer_future():
    from voidx.presentation.output.events.bus import UiEventTimeout

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
    from voidx.presentation.output.events import bus as bus_module
    from voidx.presentation.output.events.bus import UiEventTimeout

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


def test_turn_started_defaults_and_consumer_passes_metadata(isolated_dock):
    from voidx.agent.domain.turn_metadata import TurnMetadata

    default_event = TurnStarted(text="hi")
    assert default_event.metadata.protocol == "turn"
    assert default_event.metadata.profile_id == "coding"
    assert default_event.metadata.category == "coding"

    loop_metadata = TurnMetadata(profile_id="loop", protocol="loop", category="loop")
    consumer = DockEventConsumer(isolated_dock)
    consumer.handle(TurnStarted(text="[loop] visible only", metadata=loop_metadata))

    assert isolated_dock.turn_in_progress is True
    assert isolated_dock.current_turn_metadata == loop_metadata


def test_dock_turn_metadata_clears_on_end_reset_and_restore(isolated_dock):
    from voidx.agent.domain.turn_metadata import TurnMetadata
    from voidx.presentation.output.tree import OutputTree

    loop_metadata = TurnMetadata(profile_id="loop", protocol="loop", category="loop")

    isolated_dock.start_turn("loop", metadata=loop_metadata)
    isolated_dock.end_turn()
    assert isolated_dock.current_turn_metadata.protocol == "turn"

    isolated_dock.start_turn("loop", metadata=loop_metadata)
    isolated_dock.reset()
    assert isolated_dock.current_turn_metadata.protocol == "turn"

    isolated_dock.start_turn("loop", metadata=loop_metadata)
    isolated_dock.restore_tree(OutputTree())
    assert isolated_dock.current_turn_metadata.protocol == "turn"


def test_turn_node_payload_preserves_raw_submit_text(isolated_dock):
    from voidx.presentation.protocol.transcript import tree_to_snapshot

    raw = "看下这张图 [image-clipboard-20260818]"
    isolated_dock.start_turn(
        "看下这张图\n[attachments: .voidx/attachments/clipboard-20260818.png]",
        raw_text=raw,
    )

    snapshot = tree_to_snapshot(isolated_dock.tree)
    turn = next(node for node in snapshot.nodes if node.node_type == "turn")
    assert turn.payload.get("raw_text") == raw


def test_turn_node_payload_defaults_to_display_text(isolated_dock):
    from voidx.presentation.protocol.transcript import tree_to_snapshot

    isolated_dock.start_turn("plain message")

    snapshot = tree_to_snapshot(isolated_dock.tree)
    turn = next(node for node in snapshot.nodes if node.node_type == "turn")
    assert turn.payload.get("raw_text") == "plain message"


def test_guidance_turn_payload_preserves_raw_text_and_style(isolated_dock):
    from voidx.presentation.protocol.transcript import tree_to_snapshot

    raw = "继续执行 [image-clipboard-x]"
    isolated_dock.append_guidance_turn(raw)

    snapshot = tree_to_snapshot(isolated_dock.tree)
    turn = next(node for node in snapshot.nodes if node.node_type == "turn")
    assert turn.payload.get("raw_text") == raw
    assert turn.payload.get("style") == "guidance"


def test_consumer_turn_started_forwards_raw_text(isolated_dock):
    from voidx.presentation.protocol.transcript import tree_to_snapshot

    consumer = DockEventConsumer(isolated_dock)
    consumer.handle(TurnStarted(
        text="看下这张图\n[attachments: .voidx/attachments/x.png]",
        raw_text="看下这张图 [image-x]",
    ))

    snapshot = tree_to_snapshot(isolated_dock.tree)
    turn = next(node for node in snapshot.nodes if node.node_type == "turn")
    assert turn.payload.get("raw_text") == "看下这张图 [image-x]"


@pytest.mark.asyncio
async def test_event_publisher_end_turn_closes_dock_turn(isolated_dock):
    from types import SimpleNamespace

    from voidx.presentation.terminal.events import UiAgentEventPublisher

    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        publisher = UiAgentEventPublisher(SimpleNamespace(dock=isolated_dock, events=bus))
        publisher.start_turn("/model switch x")
        assert isolated_dock.turn_in_progress is True

        publisher.end_turn()
        await bus.drain()

        assert isolated_dock.turn_in_progress is False
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_event_publisher_fail_turn_closes_dock_turn(isolated_dock):
    from types import SimpleNamespace

    from voidx.presentation.terminal.events import UiAgentEventPublisher

    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        publisher = UiAgentEventPublisher(SimpleNamespace(dock=isolated_dock, events=bus))
        publisher.start_turn("/model switch x")
        assert isolated_dock.turn_in_progress is True

        publisher.fail_turn("boom")
        await bus.drain()

        assert isolated_dock.turn_in_progress is False
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_event_publisher_cancel_turn_closes_dock_turn(isolated_dock):
    from types import SimpleNamespace

    from voidx.presentation.terminal.events import UiAgentEventPublisher

    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        publisher = UiAgentEventPublisher(SimpleNamespace(dock=isolated_dock, events=bus))
        publisher.start_turn("/model switch x")
        assert isolated_dock.turn_in_progress is True

        publisher.cancel_turn()
        await bus.drain()

        assert isolated_dock.turn_in_progress is False
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_yields_after_event_count_budget(isolated_dock):
    processed: list[str] = []
    probe_snapshot: list[tuple[str, ...]] = []

    class Consumer:
        def handle(self, event):
            processed.append(event.text)

    bus = UiEventBus(batch_event_limit=2, batch_time_budget=10.0)
    bus.start(Consumer())
    try:
        for index in range(5):
            assert bus.emitnowait(TurnStarted(text=str(index)))

        probe_done = asyncio.Event()

        async def probe() -> None:
            probe_snapshot.append(tuple(processed))
            probe_done.set()

        asyncio.create_task(probe())
        await asyncio.wait_for(probe_done.wait(), timeout=0.1)

        assert probe_snapshot == [("0", "1")]
        await bus.drain()
        assert processed == ["0", "1", "2", "3", "4"]
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_yields_after_time_budget(isolated_dock):
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    processed: list[str] = []
    probe_snapshot: list[tuple[str, ...]] = []

    class Consumer:
        def handle(self, event):
            processed.append(event.text)
            clock.value += 1.0

    bus = UiEventBus(batch_event_limit=100, batch_time_budget=2.0, clock=clock)
    bus.start(Consumer())
    try:
        for index in range(5):
            assert bus.emitnowait(TurnStarted(text=str(index)))

        probe_done = asyncio.Event()

        async def probe() -> None:
            probe_snapshot.append(tuple(processed))
            probe_done.set()

        asyncio.create_task(probe())
        await asyncio.wait_for(probe_done.wait(), timeout=0.1)

        assert probe_snapshot == [("0", "1")]
        await bus.drain()
        assert processed == ["0", "1", "2", "3", "4"]
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_coalesces_streams_only_within_the_same_thread():
    received: list[tuple[str, str]] = []

    class Consumer:
        def handle(self, event):
            received.append((event.thread_id, event.text))

    bus = UiEventBus()
    bus.start(Consumer())
    try:
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="shared",
                text="a",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-b",
                stream_id="shared",
                text="b",
            )
        )
        await bus.drain()

        assert received == [("thread-a", "a"), ("thread-b", "b")]
        assert bus.metrics.coalesced == 0
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_coalesces_cumulative_stream_to_latest_text():
    received: list[tuple[str, str]] = []

    class Consumer:
        def handle(self, event):
            received.append((event.stream_id, event.text))

    bus = UiEventBus()
    bus.start(Consumer())
    try:
        for text in ("a", "ab", "abc"):
            assert bus.emitnowait(
                AssistantStreamUpdated(
                    thread_id="thread-a",
                    stream_id="stream-a",
                    text=text,
                    phase="text",
                    snapshot_contract="cumulative",
                )
            )

        await bus.drain()

        assert received == [("stream-a", "abc")]
        assert bus.metrics.coalesced == 2
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_stream_barriers_preserve_phase_contract_and_order():
    received: list[tuple[str, str, str, str]] = []

    class Consumer:
        def handle(self, event):
            received.append((event.thread_id, event.stream_id, event.phase, event.text))

    bus = UiEventBus()
    bus.start(Consumer())
    try:
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="think 1",
                phase="thinking",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="think 2",
                phase="thinking",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="answer 1",
                phase="text",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="answer 2",
                phase="text",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-b",
                text="other stream",
                phase="text",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-b",
                stream_id="stream-a",
                text="other thread",
                phase="text",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="delta 1",
                phase="text",
                snapshot_contract="delta",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="delta 2",
                phase="text",
                snapshot_contract="delta",
            )
        )

        await bus.drain()

        assert received == [
            ("thread-a", "stream-a", "thinking", "think 2"),
            ("thread-a", "stream-a", "text", "answer 2"),
            ("thread-a", "stream-b", "text", "other stream"),
            ("thread-b", "stream-a", "text", "other thread"),
            ("thread-a", "stream-a", "text", "delta 1"),
            ("thread-a", "stream-a", "text", "delta 2"),
        ]
        assert bus.metrics.coalesced == 2
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_request_future_is_a_stream_barrier():
    received: list[tuple[str, str]] = []
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    class Consumer:
        async def handle(self, event):
            if isinstance(event, TurnStarted):
                request_started.set()
                await release_request.wait()
            received.append((type(event).__name__, getattr(event, "text", "")))
            return "request-result"

    bus = UiEventBus()
    bus.start(Consumer())
    try:
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="a",
            )
        )
        request_task = asyncio.create_task(
            bus.request(TurnStarted(text="barrier"), timeout=0.5, max_retries=1)
        )
        await asyncio.wait_for(request_started.wait(), timeout=0.1)
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="ab",
            )
        )
        assert bus.emitnowait(
            AssistantStreamUpdated(
                thread_id="thread-a",
                stream_id="stream-a",
                text="abc",
            )
        )
        release_request.set()

        assert await request_task == "request-result"
        await bus.drain()

        assert received == [
            ("AssistantStreamUpdated", "a"),
            ("TurnStarted", "barrier"),
            ("AssistantStreamUpdated", "abc"),
        ]
        assert bus.metrics.coalesced == 1
    finally:
        release_request.set()
        if not request_task.done():
            request_task.cancel()
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_waits_for_stream_commit_before_following_message(
    isolated_dock, monkeypatch
):
    import threading

    from voidx.presentation.output.events import MessageAppended
    from voidx.presentation.output.events import consumers as consumers_module

    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    started = threading.Event()
    release = threading.Event()
    stats_applied = asyncio.Event()
    original_builder = consumers_module.build_canonical_stream_projection
    original_append_message = isolated_dock.append_message

    def blocked_builder(work_item):
        started.set()
        assert release.wait(1)
        return original_builder(work_item)

    def append_message(text, *args, **kwargs):
        if text == "stats":
            stats_applied.set()
        return original_append_message(text, *args, **kwargs)

    monkeypatch.setattr(
        consumers_module,
        "build_canonical_stream_projection",
        blocked_builder,
    )
    monkeypatch.setattr(isolated_dock, "append_message", append_message)

    try:
        await bus.emit(AssistantStreamUpdated(text="hello **world**"))
        await bus.emit(AssistantStreamCommitted())
        await bus.emit(MessageAppended(text="stats"))

        assert await asyncio.to_thread(started.wait, 1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stats_applied.wait(), timeout=0.05)

        release.set()
        await bus.drain()
        assert stats_applied.is_set()
    finally:
        release.set()
        await bus.stop()


def test_message_event_preserves_markup_for_dock(isolated_dock, monkeypatch):
    from voidx.presentation.output.events import MessageAppended

    isolated_dock.begin_capture()
    consumer = DockEventConsumer(isolated_dock)
    captured: dict[str, object] = {}
    original_append_message = isolated_dock.append_message

    def append_message(text, *args, **kwargs):
        captured.update(kwargs)
        return original_append_message(text, *args, **kwargs)

    monkeypatch.setattr(isolated_dock, "append_message", append_message)

    consumer.handle(MessageAppended(
        text="[dim]stats[/dim]",
        style="turn_stats",
        markup=True,
    ))

    assert captured["markup"] is True
