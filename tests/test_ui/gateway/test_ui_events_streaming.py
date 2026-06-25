import asyncio
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

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
async def test_streaming_renderer_discards_thinking_only_stream(isolated_dock):
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
            args='agent="explore"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="explore",
            description="inspect project",
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
        task_tool = next(node for node in assistant.children if node.node_type == "tool_call")
        subagent = next(node for node in task_tool.children if node.node_type == "subagent")

        # No child assistant stream node under the subagent
        stream_nodes = [n for n in subagent.children if n.node_type == "assistant"]
        assert stream_nodes == []

        # Child tool calls are still visible
        sub_tools = [n for n in subagent.children if n.node_type == "tool_call"]
        assert len(sub_tools) == 1
        assert 'Read("x.py")' in _rich_plain(sub_tools[0].header)

        # No ToolResultAppended under the parent agent tool node
        result_nodes = [n for n in task_tool.children if n.node_type == "tool_result"]
        assert result_nodes == []
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_capture_console_uses_ui_event_bus_for_subagent_tools(isolated_dock):
    isolated_dock.begin_capture()
    ui_events.start(DockEventConsumer(isolated_dock))
    try:
        parent = await ui_events.request(TurnStarted(text="demo"))
        capture = CaptureConsole(isolated_dock.tree, parent, agent_id=0)

        capture.step_header("explore")
        capture.tool_call("read", {"file_path": "x.py"})
        capture.tool_done("read", 0.0, True)
        capture.tool_result("first\nsecond")
        await ui_events.drain()

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(100))
        assert "Exploring" in rendered
        assert 'Read("x.py")' in rendered
        assert "[cyan]" not in rendered

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        tool = next(node for node in subagent.children if node.node_type == "tool_call")
        isolated_dock.tree.expand(tool.id)
        expanded = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "first" in expanded
        assert tool.children[0].body_lines == ["second"]
    finally:
        await ui_events.stop()


def test_capture_console_non_event_methods_append_under_parent(isolated_dock):
    isolated_dock.begin_capture()
    parent = isolated_dock.tree.new_node(
        isolated_dock.tree.root,
        node_type="subagent",
        header="child",
        collapsed=False,
    )
    capture = CaptureConsole(isolated_dock.tree, parent, agent_id=0)

    capture.print("[bold]hello[/bold]")
    capture.markdown("**markdown** item")
    capture.thinking("checked context")
    capture.sep()

    child_types = [node.node_type for node in parent.children]
    rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(100))

    assert child_types == ["message", "message", "thought", "message"]
    assert "hello" in rendered
    assert "markdown item" in rendered
    assert "Thinking" in rendered
    assert "checked context" in rendered
    assert "─" in rendered
    assert all(child.parent is parent for child in parent.children)


@pytest.mark.asyncio
async def test_capture_console_event_methods_remain_noop(isolated_dock):
    isolated_dock.begin_capture()
    ui_events.start(DockEventConsumer(isolated_dock))
    try:
        parent = await ui_events.request(TurnStarted(text="demo"))
        capture = CaptureConsole(isolated_dock.tree, parent, agent_id=0)

        capture.print("hello")
        capture.markdown("**markdown**")
        capture.thinking("checked context")
        capture.sep()
        await ui_events.drain()

        assert parent.children == []
        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(100))
        assert "hello" not in rendered
        assert "markdown" not in rendered
        assert "checked context" not in rendered
    finally:
        await ui_events.stop()


@pytest.mark.asyncio
async def test_subagent_tool_events_attach_under_agent_id(isolated_dock):
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
            args='agent="explore"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="explore",
            description="inspect project",
            parent_agent_id=-1,
            parent_tool_call_id="task_call",
        ))
        await bus.emit(ToolStarted(
            agent_id=0,
            tool_call_id="sub_read",
            tool_name="read",
            label="Reading",
            args='file_path="x.py"',
        ))
        await bus.emit(ToolFinished(agent_id=0, tool_call_id="sub_read", label="Read", elapsed=0.1))
        await bus.emit(ToolResultAppended(agent_id=0, tool_call_id="sub_read", text="sub result"))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        task_tool = next(node for node in assistant.children if node.node_type == "tool_call")
        subagent = next(node for node in task_tool.children if node.node_type == "subagent")
        sub_tool = next(node for node in subagent.children if node.node_type == "tool_call")

        assert "Explorer" in subagent.header
        assert subagent.payload["agent_name"] == "explore"
        sub_tool_header = _rich_plain(sub_tool.header)
        assert 'Read("x.py")' in sub_tool_header
        assert "[cyan]" not in sub_tool_header
        assert sub_tool.children[0].header == "sub result"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_child_agent_stream_and_progress_attach_under_agent_node(isolated_dock):
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
            args='agent="explore"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="explore",
            description="inspect auth.py",
            parent_tool_call_id="task_call",
        ))
        await bus.emit(SubagentStepStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="Exploring",
        ))
        await bus.emit(AssistantStreamUpdated(agent_id=0, text="● found the auth flow"))
        await bus.emit(AssistantStreamCommitted(agent_id=0))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        task_tool = next(node for node in assistant.children if node.node_type == "tool_call")
        agent_node = next(node for node in task_tool.children if node.node_type == "subagent")
        stream_node = next(node for node in agent_node.children if node.node_type == "assistant")

        assert "Explorer" in agent_node.header
        assert "agent" not in agent_node.header
        assert agent_node.body_lines == []
        assert agent_node.payload["description"] == "inspect auth.py"
        assert agent_node.payload["agent_id"] == 0
        assert "found the auth flow" in stream_node.header

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Task:" not in rendered
        assert "inspect auth.py" not in rendered
        assert "Agent ID" not in rendered
        assert "Exploring" in rendered
        assert "found the auth flow" in rendered

        await bus.emit(SubagentFinished(
            agent_id=0,
            subagent_id="agent_0",
            ok=True,
            elapsed=2.5,
            finish_reason="final_answer",
        ))
        await bus.drain()

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(100))
        assert "explore agent completed (2.5s)" not in rendered
        assert "Explorer completed (final answer, 2.5s)" in rendered
        assert "Explorer" in rendered
        assert 'Agent("explore")' not in rendered
        assert "subagent completed" not in rendered
    finally:
        await bus.stop()
