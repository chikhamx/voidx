import asyncio
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
                "Target: src/voidx/ui/output/events/consumers.py\n"
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
        assert "voidx(实现子agent任务摘要展示) completed" in _rich_plain(subagent.header)

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

