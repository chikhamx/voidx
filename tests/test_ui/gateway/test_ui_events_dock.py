import asyncio
import logging
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
    CheckpointChoicePayload,
    CheckpointDecisionSubmitted,
    CheckpointPlanPayload,
    CheckpointPromptShown,
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
            status_id="generic:status",
            label="Working",
            detail="loading context",
            stage="working",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Working" in rendered
        assert "loading context" in rendered

        await bus.emit(StatusFinished(status_id="generic:status"))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Working" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_turn_analyzing_status_records_without_transcript_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StatusUpdated(
            status_id="turn:analyzing",
            label="Analyzing",
            detail="loading context",
            stage="analyzing",
            display="record_only",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Analyzing" not in rendered
        assert "loading context" not in rendered
        assert isolated_dock.status_record("turn:analyzing").label == "Analyzing"

        await bus.emit(StatusFinished(status_id="turn:analyzing"))
        await bus.drain()

        assert isolated_dock.status_record("turn:analyzing") is None
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_compacting_status_records_without_transcript_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StatusUpdated(
            status_id="compaction",
            label="Compacting",
            detail="summarizing old messages",
            stage="compacting",
            display="record_only",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Compacting" not in rendered
        assert "summarizing old messages" not in rendered
        assert isolated_dock.status_record("compaction").label == "Compacting"

        await bus.emit(StatusFinished(status_id="compaction"))
        await bus.drain()

        assert isolated_dock.status_record("compaction") is None
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
            display="record_only",
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
async def test_permission_prompt_event_renders_and_clears(isolated_dock):
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

        record = isolated_dock.status_record("permission:request")
        assert record is not None
        assert record.label == "Requesting"
        assert "1. bash" in record.detail
        assert "target: npm test" in record.detail
        assert "command: npm test" in record.detail

        await bus.emit(PermissionPromptCleared())
        await bus.drain()

        assert isolated_dock.status_record("permission:request") is None
    finally:
        await bus.stop()

@pytest.mark.asyncio
async def test_checkpoint_prompt_event_renders_voidx_plan_and_decision(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(CheckpointPromptShown(
            checkpoint_id="cp_1",
            plan=CheckpointPlanPayload(
                plan_summary="Add checkpoint node",
                steps=["Add event schema", "Render TUI node"],
                affected_files=["src/voidx/tools/plan_checkpoint.py"],
                risks=["Do not duplicate hidden JSON result"],
            ),
            choices=[
                CheckpointChoicePayload(
                    label="Implement directly",
                    value="approved",
                    description="Start implementing the plan",
                )
            ],
        ))
        await bus.drain()

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(120))
        nodes = _tree_nodes(isolated_dock.tree.root)
        checkpoint = next(node for node in nodes if node.node_type == "checkpoint")

        assert "voidx plan" in rendered
        assert "Plan: Add checkpoint node" in rendered
        assert "1. Add event schema" in rendered
        assert "src/voidx/tools/plan_checkpoint.py" in rendered
        assert "Do not duplicate hidden JSON result" in rendered
        assert "Choices:" not in rendered
        assert "Implement directly: Start implementing the plan" not in rendered
        assert any("Plan:" in line and "#EBCB8B" in line for line in checkpoint.body_lines)
        assert any("1." in line and "#61AFEF" in line for line in checkpoint.body_lines)
        assert any("src/voidx/tools/plan_checkpoint.py" in line and "#56D4DD" in line for line in checkpoint.body_lines)
        assert any("Do not duplicate hidden JSON result" in line and "#E06C75" in line for line in checkpoint.body_lines)
        assert checkpoint.status == "running"
        assert checkpoint.payload["checkpoint_id"] == "cp_1"
        assert isolated_dock.safe_flush_line_count(120, 0) == len(isolated_dock.tree.render(120))

        await bus.emit(CheckpointDecisionSubmitted(
            checkpoint_id="cp_1",
            decision="approved",
            label="Implement directly",
            response="Implement directly",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(120))

        assert checkpoint.status == "done"
        assert "voidx plan approved" in rendered
        assert "User: Implement directly" in rendered
        assert checkpoint.payload["decision"] == "approved"
        assert checkpoint.payload["response"] == "Implement directly"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_checkpoint_decision_renders_as_full_width_user_row_with_following_gap(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(CheckpointPromptShown(
            checkpoint_id="cp_1",
            plan=CheckpointPlanPayload(plan_summary="Add checkpoint node"),
            choices=[
                CheckpointChoicePayload(
                    label="Implement directly",
                    value="approved",
                    description="Start implementing the plan",
                )
            ],
        ))
        await bus.emit(CheckpointDecisionSubmitted(
            checkpoint_id="cp_1",
            decision="approved",
            label="Implement directly",
            response="Implement directly",
        ))
        await bus.emit(AssistantStreamUpdated(text="先删除临时文件，然后开始分步 commit。"))
        await bus.drain()

        lines = isolated_dock.tree.render(80)
        plain_lines = [_rich_plain(line) for line in lines]
        user_index = plain_lines.index("User: Implement directly" + (" " * 56))

        assert Text.from_markup(lines[user_index]).cell_len == 80
        assert any("on #3a3937" in str(span.style) for span in Text.from_markup(lines[user_index]).spans)
        assert plain_lines[user_index + 1] == ""
        assert plain_lines[user_index + 2].startswith("● 先删除临时文件")
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_checkpoint_needs_doc_uses_distinct_header_style(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(CheckpointPromptShown(
            checkpoint_id="cp_doc",
            plan=CheckpointPlanPayload(plan_summary="Add checkpoint node"),
        ))
        await bus.emit(CheckpointDecisionSubmitted(
            checkpoint_id="cp_doc",
            decision="needs_doc",
            label="Document first",
            response="Document first",
        ))
        await bus.drain()

        checkpoint = next(
            node for node in _tree_nodes(isolated_dock.tree.root)
            if node.node_type == "checkpoint"
        )

        assert "[yellow]voidx plan needs_doc[/yellow]" in checkpoint.header
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_checkpoint_decision_for_unknown_id_logs_debug(isolated_dock, caplog):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        caplog.set_level(logging.DEBUG, logger="voidx.ui.output.dock.nodes_checkpoint")
        await bus.emit(CheckpointDecisionSubmitted(
            checkpoint_id="missing_cp",
            decision="approved",
            label="Implement directly",
            response="Implement directly",
        ))
        await bus.drain()

        assert "unknown checkpoint_id" in caplog.text
        assert "missing_cp" in caplog.text
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_guidance_submitted_event_does_not_render_message(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(GuidanceSubmitted(text="看可以调用LoginDevice::get_chatters"))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "LoginDevice::get_chatters" not in rendered
        assert "[guide]" not in rendered
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
