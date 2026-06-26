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
            args='agent="implement"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="implement",
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
        assert "Implementer(实现子agent任务摘要展示) completed" in _rich_plain(subagent.header)

        # No ToolResultAppended under the child agent node
        result_nodes = [n for n in subagent.children if n.node_type == "tool_result"]
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
        assert "Reading x.py" in rendered
        assert 'Read("x.py")' not in rendered
        assert "[cyan]" not in rendered

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        status = next(node for node in subagent.children if node.node_type == "status")
        isolated_dock.tree.expand(status.id)
        expanded = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "first" not in expanded
        assert status.body_lines == []
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
async def test_subagent_tool_events_update_single_status_row(isolated_dock):
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
            args='agent="implement"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="implement",
            description=(
                "Task: 实现子agent任务摘要展示\n"
                "Mode: implement\n"
                "Target: src/voidx/ui/output/events/consumers.py\n"
                "Success criteria: 标题中展示短摘要"
            ),
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
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        status_nodes = [node for node in subagent.children if node.node_type == "status"]

        assert "Implementer" in subagent.header
        assert "实现子agent任务摘要展示" in _rich_plain(subagent.header)
        assert subagent.payload["agent_name"] == "implement"
        assert len(status_nodes) == 1
        assert _rich_plain(status_nodes[0].header) == "● Reading x.py"
        assert status_nodes[0].body_lines == []
        assert not any(node.node_type == "tool_call" for node in subagent.children)

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(100))
        assert 'Read("x.py")' not in rendered
        assert "sub result" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subagent_title_ignores_mode_when_using_fallback_summary(isolated_dock):
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
            args='agent="implement"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="implement",
            description=(
                "Mode: implement\n"
                "Success criteria: update the dock title\n"
                "Check title fallback behavior"
            ),
            parent_tool_call_id="task_call",
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        title = _rich_plain(subagent.header)

        assert "Implementer(update the dock title)" in title
        assert "Implementer(implement)" not in title
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subagent_git_tool_uses_git_status_action(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="implement",
            description="update git status display",
        ))
        await bus.emit(ToolStarted(
            agent_id=0,
            tool_call_id="sub_git",
            tool_name="git",
            label="Running",
            raw_args={"args": "status --short"},
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        status = next(node for node in subagent.children if node.node_type == "status")

        assert _rich_plain(status.header) == "● Git status --short"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subagent_step_does_not_overwrite_specific_tool_status(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="review",
            description="review permission and UI changes",
        ))
        await bus.emit(SubagentStepStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="Reviewing",
        ))
        await bus.emit(ToolStarted(
            agent_id=0,
            tool_call_id="sub_read",
            tool_name="read",
            label="Reading",
            raw_args={"file_path": "src/voidx/permission/rules.py"},
        ))
        await bus.emit(SubagentStepStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="Reviewing",
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        status = next(node for node in subagent.children if node.node_type == "status")

        assert "Reviewer(review permission and UI changes)" in _rich_plain(subagent.header)
        assert _rich_plain(status.header) == "● Reading src/voidx/permission/rules.py"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_child_agent_stream_updates_status_without_rendering_text(isolated_dock):
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
        agent_node = next(node for node in assistant.children if node.node_type == "subagent")
        status_node = next(node for node in agent_node.children if node.node_type == "status")

        assert "Explorer" in agent_node.header
        assert "agent" not in agent_node.header
        assert agent_node.body_lines == []
        assert agent_node.payload["description"] == "inspect auth.py"
        assert agent_node.payload["agent_id"] == 0
        assert _rich_plain(status_node.header) == "● Responding"
        assert not any(node.node_type == "assistant" for node in agent_node.children)

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Task:" not in rendered
        assert "Explorer(inspect auth.py)" in rendered
        assert "Agent ID" not in rendered
        assert "Exploring" not in rendered
        assert "Responding" in rendered
        assert "found the auth flow" not in rendered

        await bus.emit(SubagentFinished(
            agent_id=0,
            subagent_id="agent_0",
            ok=True,
            elapsed=2.5,
            finish_reason="final_answer",
            summary="审查发现权限申请和 UI 展示还有 2 个问题，需要继续修复。" * 4,
        ))
        await bus.drain()

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(100))
        assert "explore agent completed (2.5s)" not in rendered
        assert "Explorer(inspect auth.py) completed (final answer, 2.5s)" in rendered
        assert "Explorer" in rendered
        assert 'Agent("explore")' not in rendered
        assert "subagent completed" not in rendered

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        agent_node = next(node for node in assistant.children if node.node_type == "subagent")
        status_node = next(node for node in agent_node.children if node.node_type == "status")
        status_text = _rich_plain(status_node.header)
        assert status_text.startswith("● 审查发现权限申请和 UI 展示还有 2 个问题")
        assert len(status_text) < 100
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subagent_finish_failure_keeps_reason_status_without_summary(isolated_dock):
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TurnStarted(text="demo"))
        await bus.emit(ToolStarted(
            agent_id=-1,
            tool_call_id="task_call",
            tool_name="agent",
            label="Agent",
            args='name="reviewer", description="Task: review permission flow"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="reviewer",
            description="Task: review permission flow",
            parent_agent_id=-1,
            parent_tool_call_id="task_call",
        ))
        await bus.emit(SubagentStepStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="Reviewing",
        ))
        await bus.emit(SubagentFinished(
            agent_id=0,
            subagent_id="agent_0",
            ok=False,
            elapsed=1.2,
            finish_reason="permission_denied",
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        agent_node = next(node for node in assistant.children if node.node_type == "subagent")
        status_node = next(node for node in agent_node.children if node.node_type == "status")

        assert _rich_plain(status_node.header) == "✗ Failed: permission denied"
        assert "Reviewer(review permission flow) failed (permission denied, 1.2s)" in _rich_plain(agent_node.header)
    finally:
        await bus.stop()



@pytest.mark.asyncio
async def test_reparent_status_updates_node_depth(isolated_dock):
    """When a status node is reparented via SubagentStarted, its depth must match the new parent."""
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.request(ToolStarted(
            agent_id=-1,
            tool_call_id="task_call",
            tool_name="agent",
            label="Running",
            args='agent="implement"',
        ))
        # Child agent starts streaming first — creates agent:0:progress status node
        await bus.emit(AssistantStreamUpdated(
            agent_id=0,
            text="thinking...",
            phase="thinking",
        ))
        # SubagentStarted triggers reparent of the status node under the tool node
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="implement",
            description="implement task",
            parent_agent_id=-1,
            parent_tool_call_id="task_call",
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        status_nodes = [n for n in subagent.children if n.node_type == "status"]
        assert len(status_nodes) == 1
        reparented = status_nodes[0]
        assert reparented.depth == subagent.depth + 1
    finally:
        await bus.stop()
