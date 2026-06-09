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
from voidx.ui.output.events import (
    AssistantStreamCommitted,
    AssistantStreamUpdated,
    DockEventConsumer,
    ErrorAppended,
    FileChangeAppended,
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _plain(line: str) -> str:
    return _ANSI_RE.sub("", line.replace(ANSI_LINE_PREFIX, ""))


def _rich_plain(line: str) -> str:
    return Text.from_markup(_plain(line)).plain


def test_dock_event_consumer_rejects_unsupported_event(isolated_dock):
    consumer = DockEventConsumer(isolated_dock)

    with pytest.raises(TypeError, match="Unsupported UI event"):
        consumer.handle(object())


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
async def test_ui_event_bus_commits_stream_text(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(AssistantStreamUpdated(text="● 这是 **voidx**\n\n- 支持 Markdown"))
        await bus.emit(AssistantStreamCommitted())
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "这是 voidx" in rendered
        assert "支持 Markdown" in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_startup_event_renders_structured_startup_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StartupShown(
            model="mimo-v2.5",
            provider="mimo",
            workspace="/Users/chikham/workspace/voidx",
            session_title="你好",
            is_new=False,
        ))
        await bus.drain()

        node = isolated_dock.tree.root.children[-1]
        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))

        assert node.node_type == "startup"
        assert ANSI_LINE_PREFIX not in "\n".join(isolated_dock.tree.render(100))
        assert "Welcome back!" in rendered
        assert "mimo/mimo-v2.5" in rendered
        assert "/\\________/\\    ╭╮" in rendered
        assert "Ask anything" in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_startup_event_updates_existing_startup_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StartupShown(
            model="old-model",
            provider="old-provider",
            workspace="/tmp/project",
            session_title="Old",
            is_new=True,
        ))
        await bus.emit(StartupShown(
            model="new-model",
            provider="new-provider",
            workspace="/tmp/project",
            session_title="New",
            is_new=True,
        ))
        await bus.drain()

        startup_nodes = [
            node for node in isolated_dock.tree.root.children if node.node_type == "startup"
        ]
        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))

        assert len(startup_nodes) == 1
        assert "new-provider/new-model" in rendered
        assert "old-provider/old-model" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_startup_event_includes_no_profile_notice(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StartupShown(
            model="claude-sonnet-4-6",
            provider="anthropic",
            workspace="/tmp/project",
            session_title="New session",
            is_new=True,
            profile_configured=False,
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))

        assert "Welcome to voidx!" in rendered
        assert "No profile configured" in rendered
        assert "/model new" in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_status_events_render_and_clear(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StatusUpdated(
            status_id="turn:analyzing",
            label="Analyzing",
            detail="loading context",
            stage="analyzing",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Analyzing" in rendered
        assert "loading context" in rendered

        await bus.emit(StatusFinished(status_id="turn:analyzing"))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Analyzing" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_agent_step_status_updates_panel_without_transcript_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StatusUpdated(
            status_id="agent:-1:progress",
            label="Agent step 1/50",
            stage="agent_step",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Agent step" not in rendered
        assert isolated_dock.status_record("agent:-1:progress").label == "Agent step 1/50"

        await bus.emit(StatusFinished(status_id="agent:-1:progress"))
        await bus.drain()

        assert isolated_dock.status_record("agent:-1:progress") is None
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_permission_prompt_event_does_not_pollute_transcript(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(PermissionPromptShown(
            prompt="Allow tools: bash?",
            choices=[("Once", "y", "Allow once")],
            tools=[
                PermissionToolDetail(
                    name="bash",
                    pattern="npm test",
                    args={"command": "npm test"},
                )
            ],
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Permission required" not in rendered
        assert "Allow tools: bash?" not in rendered
        assert "npm test" not in rendered

        await bus.emit(PermissionPromptCleared())
        await bus.drain()
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_error_event_renders_as_aligned_message_without_panel(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(ErrorAppended(
            message="LLM call failed after 3 attempts: name 'resolve_protocol' is not defined\nretry aborted",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(120))
        node = isolated_dock.tree.root.children[-1]

        assert node.node_type == "error"
        assert "LLM call failed after 3 attempts" in rendered
        assert "retry aborted" in rendered
        assert "╭" not in rendered
        assert "╰" not in rendered
        assert "─ error" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_file_change_event_updates_tool_node_with_structured_diff(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        tool = await bus.request(ToolStarted(
            tool_call_id="edit_call",
            tool_name="edit",
            label="Editing",
            args='file_path="[cyan]test.cpp[/cyan]"',
            raw_args={"file_path": "test.cpp"},
        ))
        await bus.emit(FileChangeAppended(
            tool_call_id="edit_call",
            diff_text="""--- a/test.cpp
+++ b/test.cpp
@@ -1,2 +1,2 @@
-old
+new
 keep
""",
        ))
        await bus.drain()
        # Edit nodes should be expanded by default, showing diff content
        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(120))
        assert 'Update("test.cpp")' in rendered
        assert "[cyan]" not in rendered
        assert "Added 1 line, removed 1 line" in rendered
        assert "-  old" in rendered
        assert "+  new" in rendered
        assert "old" in rendered
        assert "new" in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_updated_sets_pinned_state_until_committed(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.request(ToolStarted(
            tool_call_id="todo_call",
            tool_name="todo",
            label="Updating",
            args="",
        ))
        await bus.emit(TodoUpdated(
            items=[
                TodoItemPayload(content="implement event", status="in_progress"),
                TodoItemPayload(content="write tests", status="pending"),
            ],
            summary="0/2 done · 1 active · 1 pending",
        ))
        await bus.emit(TodoUpdated(
            items=[
                TodoItemPayload(content=f"task {idx}", status="pending")
                for idx in range(10)
            ],
            summary="0/10 done · 0 active · 10 pending",
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        todo_nodes = [node for node in isolated_dock.tree.root.children if node.node_type == "todo"]
        state = isolated_dock.todo_state()

        assert state is not None
        assert state.summary == "0/10 done · 0 active · 10 pending"
        assert len(state.items) == 10
        assert todo_nodes == []
        assert not any(node.node_type == "todo" for node in assistant.children)

        await bus.emit(TodoCommitted())
        await bus.drain()

        todo_nodes = [node for node in isolated_dock.tree.root.children if node.node_type == "todo"]
        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(120))

        assert isolated_dock.todo_state() is None
        assert len(todo_nodes) == 1
        assert isolated_dock.tree.root.children[-1] is todo_nodes[0]
        assert todo_nodes[0].payload["summary"] == "0/10 done · 0 active · 10 pending"
        assert len(todo_nodes[0].payload["items"]) == 10
        assert "Todo: 0/10 done" in rendered
        assert "task 7" in rendered
        assert "2 more todos" in rendered
        assert "task 8" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_updated_sets_pinned_todo_state(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TodoUpdated(
            items=[
                TodoItemPayload(content="implement pinned display", status="in_progress"),
                TodoItemPayload(content="write tests", status="pending"),
            ],
            summary="0/2 done · 1 active · 1 pending",
        ))
        await bus.drain()

        state = isolated_dock.todo_state()
        todo_nodes = [node for node in isolated_dock.tree.root.children if node.node_type == "todo"]

        assert state is not None
        assert state.summary == "0/2 done · 1 active · 1 pending"
        assert [(item.content, item.status) for item in state.items] == [
            ("implement pinned display", "in_progress"),
            ("write tests", "pending"),
        ]
        assert todo_nodes == []
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_cleared_removes_pinned_todo_without_transcript_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(content="temporary task", status="in_progress")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.emit(TodoCleared())
        await bus.drain()

        assert isolated_dock.todo_state() is None
        assert not any(node.node_type == "todo" for node in isolated_dock.tree.root.children)
    finally:
        await bus.stop()


def test_restore_tree_does_not_hydrate_committed_todo_state(isolated_dock):
    tree = OutputTree()
    tree.new_node(
        parent=tree.root,
        node_type="todo",
        header="Todo",
        collapsed=False,
        status="done",
        payload={
            "summary": "1/2 done · 0 active · 1 pending",
            "items": [
                {"content": "done task", "status": "completed"},
                {"content": "next task", "status": "pending"},
            ],
        },
    )
    tree.new_node(
        parent=tree.root,
        node_type="todo",
        header="Todo",
        collapsed=False,
        status="done",
        payload={
            "summary": "stale todo",
            "items": [
                {"content": "stale task", "status": "pending"},
            ],
        },
    )

    isolated_dock.restore_tree(tree)

    assert isolated_dock.todo_state() is None


def test_reset_clears_pinned_todo_state(isolated_dock):
    isolated_dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "in_progress"}],
    )

    isolated_dock.reset()

    assert isolated_dock.todo_state() is None


@pytest.mark.asyncio
async def test_todo_committed_appends_each_turn_todo_and_clears_pinned(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="first"))
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(content="first task", status="pending")],
            summary="0/1 done · 0 active · 1 pending",
        ))
        await bus.emit(TodoCommitted())
        await bus.request(TurnStarted(text="second"))
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(content="second task", status="in_progress")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.emit(TodoCommitted())
        await bus.drain()

        todo_nodes = [node for node in isolated_dock.tree.root.children if node.node_type == "todo"]

        assert isolated_dock.todo_state() is None
        assert len(todo_nodes) == 2
        assert todo_nodes[0].payload["summary"] == "0/1 done · 0 active · 1 pending"
        assert todo_nodes[0].payload["items"] == [
            {"content": "first task", "status": "pending"}
        ]
        assert isolated_dock.tree.root.children[-1] is todo_nodes[1]
        assert todo_nodes[1].payload["summary"] == "0/1 done · 1 active · 0 pending"
        assert todo_nodes[1].payload["items"] == [
            {"content": "second task", "status": "in_progress"}
        ]
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_updated_preserves_existing_root_order_until_commit(isolated_dock):
    isolated_dock.begin_capture()
    root = isolated_dock.tree.root
    existing = isolated_dock.tree.new_node(
        parent=root,
        node_type="message",
        header="older output",
        collapsed=False,
        status="done",
    )
    todo = isolated_dock.tree.new_node(
        parent=root,
        node_type="todo",
        header="old todo",
        collapsed=False,
        status="done",
    )
    assert root.children == [existing, todo]

    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(content="front task", status="pending")],
            summary="0/1 done · 0 active · 1 pending",
        ))
        await bus.drain()

        assert root.children == [existing, todo]
        assert isolated_dock.todo_state() is not None

        await bus.emit(TodoCommitted())
        await bus.drain()

        todo_nodes = [node for node in root.children if node.node_type == "todo"]
        assert root.children[:2] == [existing, todo]
        assert root.children[-1] is todo_nodes[-1]
        assert todo_nodes[-1].payload["summary"] == "0/1 done · 0 active · 1 pending"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_updated_with_agent_id_updates_global_root_todo(isolated_dock):
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
        await bus.emit(TodoUpdated(
            agent_id=0,
            items=[TodoItemPayload(content="inspect auth", status="in_progress")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        task_tool = next(node for node in assistant.children if node.node_type == "tool_call")
        subagent = next(node for node in task_tool.children if node.node_type == "subagent")

        assert not any(node.node_type == "todo" for node in isolated_dock.tree.root.children)
        await bus.emit(TodoCommitted())
        await bus.drain()

        todo = next(node for node in isolated_dock.tree.root.children if node.node_type == "todo")

        assert isolated_dock.tree.root.children[-1] is todo
        assert todo.payload["items"] == [{"content": "inspect auth", "status": "in_progress"}]
        assert not any(node.node_type == "todo" for node in assistant.children)
        assert not any(node.node_type == "todo" for node in task_tool.children)
        assert not any(node.node_type == "todo" for node in subagent.children)
    finally:
        await bus.stop()


@pytest.mark.asyncio
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
        assert "Thinking" in rendered
        assert "inspect auth" in rendered

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
        assert "Thinking" in rendered
        assert "two" in rendered
        assert "six" in rendered
        assert "one" not in rendered

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
        assert "Thinking" in rendered
        assert "temporary thought" in rendered

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
            step=1,
            max_steps=3,
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

        capture.step_header(1, 2, "explore")
        capture.tool_call("read", {"file_path": "x.py"})
        capture.tool_done("read", 0.0, True)
        capture.tool_result("first\nsecond")
        await ui_events.drain()

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(100))
        assert "Exploring (1/2)" in rendered
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
            step=1,
            max_steps=3,
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
        assert "Exploring (1/3)" in rendered
        assert "found the auth flow" in rendered

        await bus.emit(SubagentFinished(
            agent_id=0,
            subagent_id="agent_0",
            ok=True,
            elapsed=2.5,
        ))
        await bus.drain()

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(100))
        assert "explore agent completed (2.5s)" not in rendered
        assert "Explorer" in rendered
        assert 'Agent("explore")' not in rendered
        assert "subagent completed" not in rendered
    finally:
        await bus.stop()
