import asyncio
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text


from voidx.presentation.output.agent_display import subagent_display_name
from voidx.presentation.output.capture import CaptureConsole
from voidx.presentation.output.console import StreamingRenderer
from voidx.presentation.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.presentation.output.display_policy import ToolDisplayMode
from voidx.presentation.output.events import (
    AssistantStreamCommitted,
    AssistantStreamDiscarded,
    AssistantStreamStarted,
    AssistantStreamUpdated,
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


async def test_streaming_renderer_uses_ui_event_bus(isolated_dock):
    isolated_dock.begin_capture()
    ui_events.start(DockEventConsumer(isolated_dock))
    try:
        renderer = StreamingRenderer(Console(), debug=True)
        renderer.feed_text("这是 **voidx**")
        renderer.done()
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "这是 voidx" in rendered
    finally:
        await ui_events.stop()


@pytest.mark.asyncio
async def test_streaming_renderer_updates_thinking_and_streaming_status(isolated_dock):
    isolated_dock.begin_capture()
    ui_events.start(DockEventConsumer(isolated_dock))
    try:
        renderer = StreamingRenderer(Console(), debug=False)
        renderer.start()
        renderer.feed_thinking("inspect auth\n")
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "inspect auth" in rendered
        assert "Thinking" not in rendered

        renderer.feed_text("hello")
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Thinking" not in rendered
        assert "Thinking for" not in rendered
        assert "Streaming" not in rendered
        assert "inspect auth" not in rendered
        assert "hello" in rendered

        renderer.done()
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Streaming" not in rendered
        assert "Thinking for" not in rendered
        assert "hello" in rendered
    finally:
        await ui_events.stop()


@pytest.mark.asyncio
async def test_streaming_renderer_collapses_thinking_content_after_text_starts(isolated_dock):
    isolated_dock.begin_capture()
    ui_events.start(DockEventConsumer(isolated_dock))
    try:
        renderer = StreamingRenderer(Console(), debug=False)
        renderer.start()
        renderer.feed_thinking("one\ntwo\nthree\nfour\nfive\nsix\n")
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "two" in rendered
        assert "six" in rendered
        assert "one" not in rendered
        assert "Thinking" not in rendered

        renderer.feed_text("answer")
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Thinking for" not in rendered
        assert "Thinking" not in rendered
        assert "six" not in rendered
        assert "answer" in rendered

        renderer.done()
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Thinking" not in rendered
        assert "six" not in rendered
        assert "answer" in rendered
    finally:
        await ui_events.stop()


@pytest.mark.asyncio
async def test_streaming_renderer_commits_thinking_only_stream(isolated_dock):
    isolated_dock.begin_capture()
    ui_events.start(DockEventConsumer(isolated_dock))
    try:
        renderer = StreamingRenderer(Console(), debug=False)
        renderer.start()
        renderer.feed_thinking("temporary thought\n")
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "temporary thought" in rendered
        assert "Thinking" not in rendered

        renderer.done()
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Thinking" not in rendered
        assert "temporary thought" not in rendered
    finally:
        await ui_events.stop()


@pytest.mark.asyncio
async def test_streaming_renderer_headless_suppresses_ui_output(isolated_dock):
    isolated_dock.begin_capture()
    ui_events.start(DockEventConsumer(isolated_dock))
    try:
        renderer = StreamingRenderer(Console(), debug=False, headless=True)
        renderer.start()
        renderer.feed_thinking("hidden thought\n")
        renderer.feed_text("hidden answer")
        result = renderer.done()
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert result == "hidden answer"
        assert "hidden thought" not in rendered
        assert "hidden answer" not in rendered
        assert "Thinking" not in rendered
    finally:
        await ui_events.stop()

    renderer = StreamingRenderer(Console(), debug=False, stream_to_dock=False, headless=True)
    renderer.feed_thinking("quiet thought\n")
    renderer.feed_text("quiet answer")
    assert renderer.done() == "quiet answer"


def test_streaming_renderer_done_is_idempotent_for_dock_stream(isolated_dock):
    isolated_dock.begin_capture()
    renderer = StreamingRenderer(Console(), debug=False)
    renderer.feed_text("final answer")

    assert renderer.done() == "final answer"
    assert renderer.done() == ""

    rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
    assert rendered.count("final answer") == 1


@pytest.mark.asyncio
async def test_duplicate_stream_commit_after_permission_clear_is_ignored(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        text = "● 现在我对变更有了清晰的了解。让我先运行测试确认这些修改没有破坏什么。"
        await bus.emit(TurnStarted(text="demo"))
        await bus.emit(AssistantStreamUpdated(text=text))
        await bus.emit(AssistantStreamCommitted())
        await bus.emit(PermissionPromptShown(
            prompt="Allow tools: bash?",
            choices=[],
            tools=[
                PermissionToolDetail(
                    name="bash",
                    pattern="pytest",
                    args={"command": "pytest"},
                )
            ],
        ))
        await bus.emit(PermissionPromptCleared())
        await bus.emit(AssistantStreamUpdated(text=text))
        await bus.emit(AssistantStreamCommitted())
        await bus.emit(ToolStarted(
            tool_call_id="pytest",
            tool_name="bash",
            label="Bash",
            args='command="pytest"',
            raw_args={"command": "pytest"},
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert rendered.count("现在我对变更") == 1
        assert "Bash" in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_duplicate_text_stream_after_intervening_thinking_is_ignored(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        text = "● Now let me run the tests to verify they pass."
        await bus.emit(TurnStarted(text="demo"))
        await bus.emit(AssistantStreamUpdated(text=text))
        await bus.emit(AssistantStreamCommitted())
        await bus.emit(ToolStarted(
            tool_call_id="pytest",
            tool_name="bash",
            label="Bash",
            args='command="pytest"',
            raw_args={"command": "pytest"},
        ))
        await bus.emit(ToolFinished(tool_call_id="pytest", label="Bash", elapsed=0.1, ok=True))
        await bus.emit(AssistantStreamUpdated(
            text="No timeout plugin, let me just run without it.",
            phase="thinking",
        ))
        await bus.emit(AssistantStreamUpdated(text=text))
        await bus.emit(AssistantStreamCommitted())
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert rendered.count("Now let me run the tests") == 1
        assert "No timeout plugin" in rendered
        assert "Thinking" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subagent_streaming_is_headless(isolated_dock):
    """Simulate the event sequence a headless subagent would produce.

    In headless mode the child StreamingRenderer emits no stream events,
    so the dock tree should have no child assistant stream node.
    The parent agent tool does NOT emit ToolResultAppended — child output
    is suppressed to avoid duplicate display.
    """
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.request(ToolStarted(
            agent_id=-1,
            tool_call_id="task_call",
            tool_name="agent",
            label="Running",
            args='name="voidx"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="voidx",
            description=(
                "Task: 实现子agent任务摘要展示\n"
                "Mode: implement\n"
                "Target: src/voidx/presentation/output/events/consumers.py\n"
                "Success criteria: 标题中展示短摘要"
            ),
            parent_agent_id=-1,
            parent_tool_call_id="task_call",
        ))
        await bus.emit(SubagentStepStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="Exploring",
        ))
        # Child agent tools still emit events (CaptureConsole is not headless)
        await bus.emit(ToolStarted(
            agent_id=0,
            tool_call_id="sub_read",
            tool_name="read",
            label="Reading",
            args='file_path="x.py"',
        ))
        await bus.emit(ToolFinished(agent_id=0, tool_call_id="sub_read", label="Read", elapsed=0.1))
        await bus.emit(ToolResultAppended(agent_id=0, tool_call_id="sub_read", text="sub result"))
        # NO AssistantStreamUpdated / AssistantStreamCommitted — headless suppresses them
        await bus.emit(SubagentFinished(
            agent_id=0,
            subagent_id="agent_0",
            ok=True,
            elapsed=2.5,
        ))
        # NO ToolResultAppended for the parent agent tool — suppressed
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")

        # No child assistant stream node under the subagent
        stream_nodes = [n for n in subagent.children if n.node_type == "assistant"]
        assert stream_nodes == []

        # Child tool calls do not leave visible history after the agent finishes.
        sub_tools = [n for n in subagent.children if n.node_type == "tool_call"]
        status_nodes = [n for n in subagent.children if n.node_type == "status"]
        assert sub_tools == []
        assert len(status_nodes) == 1
        assert _rich_plain(status_nodes[0].header) == "● Completed"
        display = subagent_display_name("agent_0")
        assert f"{display} · implement(实现子agent任务摘要展示) completed" in _rich_plain(subagent.header)
        assert "voidx(" not in _rich_plain(subagent.header)

        # No ToolResultAppended under the child agent node
        result_nodes = [n for n in subagent.children if n.node_type == "tool_result"]
        assert result_nodes == []
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subagent_manage_status_uses_action_display(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.request(ToolStarted(
            agent_id=-1,
            tool_call_id="task_call",
            tool_name="agent",
            label="Running",
            args='name="voidx"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="implement",
            description="Task: 更新 manage 显示",
            parent_agent_id=-1,
            parent_tool_call_id="task_call",
        ))
        await bus.emit(ToolStarted(
            agent_id=0,
            tool_call_id="sub_manage",
            tool_name="manage",
            label="Managing",
            args='op="move"',
            raw_args={
                "op": "move",
                "moves": [{"src": "src/old.py", "dest": "src/new.py"}],
            },
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        status = isolated_dock.status_record("agent:0:progress")

        assert status is not None
        assert status.label == "Rename old.py → new.py"
        assert "Managing" not in _rich_plain(subagent.header)
    finally:
        await bus.stop()




def test_prepare_stream_commit_blocks_scrollback_until_canonical_apply(isolated_dock):
    isolated_dock.begin_capture()
    isolated_dock.set_stream("hello **world**")

    work_item = isolated_dock.prepare_stream_commit()

    assert work_item is not None
    assert work_item.raw_text == "hello **world**"
    assert work_item.phase == "text"
    lines = isolated_dock.tree.render(100)
    assert isolated_dock.safe_flush_line_count(100, 0) < len(lines)

    from voidx.presentation.output.dock.stream import build_canonical_stream_projection

    projection = build_canonical_stream_projection(work_item)
    assert isolated_dock.apply_stream_commit(work_item, projection)
    assert isolated_dock.safe_flush_line_count(100, 0) == len(
        isolated_dock.tree.render(100)
    )
    rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
    assert "hello world" in rendered
    node = isolated_dock.tree.get(work_item.node_id)
    assert node is not None
    assert not node.payload.get("render_pending")


def test_stream_commit_result_is_ignored_after_reset(isolated_dock):
    isolated_dock.begin_capture()
    isolated_dock.set_stream("old answer")
    work_item = isolated_dock.prepare_stream_commit()

    assert work_item is not None
    from voidx.presentation.output.dock.stream import build_canonical_stream_projection

    projection = build_canonical_stream_projection(work_item)
    isolated_dock.reset()

    assert isolated_dock.apply_stream_commit(work_item, projection) is False
    assert isolated_dock.tree.root.children == []


@pytest.mark.asyncio
async def test_dock_event_consumer_schedules_stream_commit_without_waiting(
    isolated_dock, monkeypatch
):
    import threading

    from voidx.presentation.output.events import consumers as consumers_module

    isolated_dock.begin_capture()
    consumer = consumers_module.DockEventConsumer(isolated_dock)
    consumer.handle(AssistantStreamUpdated(text="hello **world**"))
    started = threading.Event()
    release = threading.Event()
    original_builder = consumers_module.build_canonical_stream_projection

    def blocked_builder(work_item):
        started.set()
        assert release.wait(1)
        return original_builder(work_item)

    monkeypatch.setattr(
        consumers_module,
        "build_canonical_stream_projection",
        blocked_builder,
    )

    assert consumer.handle(AssistantStreamCommitted()) is True
    assert await asyncio.to_thread(started.wait, 1)
    assert consumer.pending_stream_commit_count == 1

    release.set()
    await consumer.drain_stream_commits()

    rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
    assert "hello world" in rendered
    assert consumer.pending_stream_commit_count == 0


@pytest.mark.asyncio
async def test_stream_commit_worker_failure_installs_escaped_fallback(
    isolated_dock, monkeypatch
):
    from voidx.presentation.output.events import consumers as consumers_module

    isolated_dock.begin_capture()
    consumer = consumers_module.DockEventConsumer(isolated_dock)
    consumer.handle(AssistantStreamUpdated(text="hello **world**"))

    def fail_builder(_work_item):
        raise ValueError("markdown worker failed")

    monkeypatch.setattr(
        consumers_module,
        "build_canonical_stream_projection",
        fail_builder,
    )
    assert consumer.handle(AssistantStreamCommitted()) is True
    await consumer.drain_stream_commits()

    rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
    assert "hello **world**" in rendered
    assert all(
        not node.payload.get("render_pending")
        for node in isolated_dock.tree.root.children
    )


@pytest.mark.asyncio
async def test_stream_commit_worker_result_cannot_repopulate_reset_tree(
    isolated_dock, monkeypatch
):
    import threading

    from voidx.presentation.output.events import consumers as consumers_module

    isolated_dock.begin_capture()
    consumer = consumers_module.DockEventConsumer(isolated_dock)
    consumer.handle(AssistantStreamUpdated(text="stale answer"))
    started = threading.Event()
    release = threading.Event()
    original_builder = consumers_module.build_canonical_stream_projection

    def blocked_builder(work_item):
        started.set()
        assert release.wait(1)
        return original_builder(work_item)

    monkeypatch.setattr(
        consumers_module,
        "build_canonical_stream_projection",
        blocked_builder,
    )
    assert consumer.handle(AssistantStreamCommitted()) is True
    assert await asyncio.to_thread(started.wait, 1)

    isolated_dock.reset()
    release.set()
    await consumer.drain_stream_commits()

    assert isolated_dock.tree.root.children == []
    assert consumer.pending_stream_commit_count == 0



def test_streaming_renderer_emits_cumulative_snapshots_before_commit(
    isolated_dock,
    monkeypatch,
):
    isolated_dock.begin_capture()
    emitted = []

    def capture(event):
        emitted.append(event)
        return True

    monkeypatch.setattr(ui_events, "emitnowait", capture)
    monkeypatch.setattr(StreamingRenderer, "FLUSH_INTERVAL", 0.0)
    renderer = StreamingRenderer(Console(), debug=False)

    renderer.start()
    renderer.feed_thinking("inspect ")
    renderer.feed_thinking("auth")
    renderer.feed_text("ans")
    renderer.feed_text("wer")
    result = renderer.done()

    updates = [event for event in emitted if isinstance(event, AssistantStreamUpdated)]

    assert [(event.phase, event.text) for event in updates] == [
        ("thinking", "inspect "),
        ("thinking", "inspect auth"),
        ("text", "● ans"),
        ("text", "● answer"),
        ("text", "● answer"),
    ]
    assert all(event.snapshot_contract == "cumulative" for event in updates)
    assert [type(event) for event in emitted] == [
        AssistantStreamStarted,
        AssistantStreamUpdated,
        AssistantStreamUpdated,
        AssistantStreamUpdated,
        AssistantStreamUpdated,
        AssistantStreamUpdated,
        AssistantStreamCommitted,
    ]
    assert result == "answer"


def test_streaming_renderer_discard_emits_no_commit(isolated_dock, monkeypatch):
    isolated_dock.begin_capture()
    emitted = []

    def capture(event):
        emitted.append(event)
        return True

    monkeypatch.setattr(ui_events, "emitnowait", capture)
    renderer = StreamingRenderer(Console(), debug=False)

    renderer.start()
    renderer.feed_text("discarded answer")
    renderer.discard()
    assert renderer.done() == "discarded answer"

    assert any(isinstance(event, AssistantStreamDiscarded) for event in emitted)
    assert not any(isinstance(event, AssistantStreamCommitted) for event in emitted)
