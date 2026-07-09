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
        assert "Decision: Implement directly" in rendered
        assert "User: Implement directly" not in rendered
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
        decision_text = "   Decision: Implement directly"
        user_index = plain_lines.index(decision_text + (" " * (80 - len(decision_text))))
        plan_line = next(line for line in plain_lines if "Plan:" in line)
        decision_line = plain_lines[user_index]

        assert Text.from_markup(lines[user_index]).cell_len == 80
        assert any("on #3a3937" in str(span.style) for span in Text.from_markup(lines[user_index]).spans)
        assert plan_line.index("Plan:") == decision_line.index("Decision:")
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
async def test_clarify_prompt_event_renders_voidx_clarify_and_answer(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(ClarifyPromptShown(
            clarify_id="cl_1",
            question="Which approach should I take?",
            options=["implement directly", "document first"],
        ))
        await bus.drain()

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(120))
        nodes = _tree_nodes(isolated_dock.tree.root)
        clarify = next(node for node in nodes if node.node_type == "clarify")

        assert "voidx clarify" in rendered
        assert "Question: Which approach should I take?" in rendered
        assert "Suggestions" in rendered
        assert "implement directly" in rendered
        assert "document first" in rendered
        assert any("Question:" in line and "#EBCB8B" in line for line in clarify.body_lines)
        assert any("-" in line and "#61AFEF" in line for line in clarify.body_lines)
        assert clarify.status == "running"
        assert clarify.payload["clarify_id"] == "cl_1"
        assert clarify.payload["question"] == "Which approach should I take?"
        assert clarify.payload["options"] == ["implement directly", "document first"]
        assert isolated_dock.safe_flush_line_count(120, 0) == len(isolated_dock.tree.render(120))

        await bus.emit(ClarifyAnswerSubmitted(
            clarify_id="cl_1",
            answer="implement directly",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(120))

        assert clarify.status == "done"
        assert "voidx clarify answered" in rendered
        assert "Answer: implement directly" in rendered
        assert "User: implement directly" not in rendered
        assert clarify.payload["answer"] == "implement directly"
        assert clarify.payload["cancelled"] is False
        assert clarify.payload["was_custom_input"] is True
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_clarify_answer_renders_as_full_width_user_row_with_following_gap(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(ClarifyPromptShown(
            clarify_id="cl_1",
            question="Which approach?",
            options=["implement", "document"],
        ))
        await bus.emit(ClarifyAnswerSubmitted(
            clarify_id="cl_1",
            answer="implement",
        ))
        await bus.emit(AssistantStreamUpdated(text="开始实现方案。"))
        await bus.drain()

        lines = isolated_dock.tree.render(80)
        plain_lines = [_rich_plain(line) for line in lines]
        answer_text = "   Answer: implement"
        user_index = plain_lines.index(answer_text + (" " * (80 - len(answer_text))))
        question_line = next(line for line in plain_lines if "Question:" in line)
        answer_line = plain_lines[user_index]

        assert Text.from_markup(lines[user_index]).cell_len == 80
        assert any("on #3a3937" in str(span.style) for span in Text.from_markup(lines[user_index]).spans)
        assert question_line.index("Question:") == answer_line.index("Answer:")
        assert plain_lines[user_index + 1] == ""
        assert plain_lines[user_index + 2].startswith("● 开始实现方案")
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_clarify_cancelled_renders_skipped_header(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(ClarifyPromptShown(
            clarify_id="cl_skip",
            question="Which approach?",
            options=["implement"],
        ))
        await bus.emit(ClarifyAnswerSubmitted(
            clarify_id="cl_skip",
            answer="",
            cancelled=True,
        ))
        await bus.drain()

        clarify = next(
            node for node in _tree_nodes(isolated_dock.tree.root)
            if node.node_type == "clarify"
        )

        assert "[red]voidx clarify skipped[/red]" in clarify.header
        assert clarify.status == "done"
        assert clarify.payload["cancelled"] is True
        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(120))
        assert "Answer: skipped" in rendered
        assert "User: skipped" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_clarify_answer_for_unknown_id_logs_debug(isolated_dock, caplog):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        caplog.set_level(logging.DEBUG, logger="voidx.ui.output.dock.nodes_clarify")
        await bus.emit(ClarifyAnswerSubmitted(
            clarify_id="missing_cl",
            answer="implement",
        ))
        await bus.drain()

        assert "unknown clarify_id" in caplog.text
        assert "missing_cl" in caplog.text
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_clarify_prompt_without_options_renders_question_only(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(ClarifyPromptShown(
            clarify_id="cl_open",
            question="What is the target framework?",
            options=[],
        ))
        await bus.drain()

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(120))
        clarify = next(
            node for node in _tree_nodes(isolated_dock.tree.root)
            if node.node_type == "clarify"
        )

        assert "Question: What is the target framework?" in rendered
        assert "Suggestions" not in rendered
        assert clarify.payload["options"] == []
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
