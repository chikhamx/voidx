import asyncio
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.ui.capture import CaptureConsole
from voidx.ui.console import StreamingRenderer
from voidx.ui.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.events import (
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
    SubagentStarted,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    TurnStarted,
    UiEventBus,
    ui_events,
)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _plain(line: str) -> str:
    return _ANSI_RE.sub("", line.replace(ANSI_LINE_PREFIX, ""))


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

        reading = next(node for header, node in tools.items() if "Reading" in header)
        mapping = next(node for header, node in tools.items() if "Mapping" in header)
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
        assert "/model config" in rendered
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
        await bus.request(ToolStarted(
            tool_call_id="edit_call",
            tool_name="edit",
            label="Editing",
            args='file_path="test.cpp"',
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

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(120))
        assert "Update" in rendered
        assert "(test.cpp)" in rendered
        assert "Added 1 line, removed 1 line" in rendered
        assert "old" in rendered
        assert "new" in rendered
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
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Thinking" in rendered

        renderer.feed_text("hello")
        await ui_events.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Thinking for" not in rendered
        assert "Streaming" in rendered
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
        assert "Thinking for" in rendered
        assert "six" not in rendered
        assert "answer" in rendered
    finally:
        await ui_events.stop()


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

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Exploring (1/2)" in rendered
        assert "Reading" in rendered
        assert "first" in rendered

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        tool = next(node for node in subagent.children if node.node_type == "tool_call")
        assert tool.children[0].body_lines == ["second"]
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
            tool_name="task",
            label="Running",
            args='subagent_type="explore"',
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

        assert "explore" in subagent.header
        assert "Reading" in sub_tool.header
        assert sub_tool.children[0].header == "sub result"
    finally:
        await bus.stop()
