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
async def test_llm_retry_status_records_without_transcript_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StatusUpdated(
            status_id="llm:retry",
            label="Retrying",
            detail="retrying in 4s: provider timeout",
            stage="working",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Retrying" not in rendered
        assert "provider timeout" not in rendered
        assert isolated_dock.status_record("llm:retry").label == "Retrying"

        await bus.emit(StatusFinished(status_id="llm:retry"))
        await bus.drain()

        assert isolated_dock.status_record("llm:retry") is None
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_error_event_clears_active_llm_retry_status(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StatusUpdated(
            status_id="llm:retry",
            label="Retrying",
            detail="retrying in 4s: provider timeout",
            stage="working",
        ))
        await bus.drain()
        assert isolated_dock.status_record("llm:retry") is not None

        await bus.emit(ErrorAppended(message="provider failed"))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert isolated_dock.status_record("llm:retry") is None
        assert "provider failed" in rendered
        assert "Retrying" not in rendered
        assert "retrying in 4s" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_error_event_records_error_current_status(isolated_dock):
    """ErrorAppended must record error:current so active_error_text() is non-empty."""
    from voidx.ui.output.dock import active_error_text, active_error_detail_text

    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(ErrorAppended(message="provider failed"))
        await bus.drain()

        assert isolated_dock.status_record("error:current") is not None
        assert active_error_text() == "Error"
        assert active_error_detail_text() == "provider failed"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_turn_started_clears_error_current_status(isolated_dock):
    """A new turn must clear the error:current record so it doesn't linger."""
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(ErrorAppended(message="provider failed"))
        await bus.drain()
        assert isolated_dock.status_record("error:current") is not None

        await bus.emit(TurnStarted(text="next turn"))
        await bus.drain()
        assert isolated_dock.status_record("error:current") is None
    finally:
        await bus.stop()


