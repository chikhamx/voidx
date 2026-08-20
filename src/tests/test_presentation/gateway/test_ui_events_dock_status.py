import asyncio
import logging
import re
import sys
from pathlib import Path

import pytest

from voidx.agent.domain.ui_events import ContextPressureFinished, ContextPressureUpdated
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
    TurnCompleted,
    TurnCancelled,
    TurnFailed,
    UiEventBus,
    ui_events,
)
from voidx.presentation.output.tree import OutputTree

from tests.test_presentation.gateway.conftest import _plain, _rich_plain, _tree_nodes, isolated_dock

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
async def test_ui_event_bus_commits_thinking_only_stream(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(AssistantStreamUpdated(text="checking permissions", phase="thinking"))
        await bus.emit(AssistantStreamCommitted())
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        nodes = _tree_nodes(isolated_dock.tree.root)
        thinking_nodes = [
            node
            for node in nodes
            if node.node_type == "assistant"
            and node.payload.get("phase") == "thinking"
        ]

        assert "checking permissions" not in rendered
        assert thinking_nodes == []
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_ui_event_bus_preserves_thinking_when_text_stream_commits(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(AssistantStreamUpdated(text="checking context", phase="thinking"))
        await bus.emit(AssistantStreamUpdated(text="● final answer", phase="text"))
        await bus.emit(AssistantStreamCommitted())
        await bus.drain()

        nodes = _tree_nodes(isolated_dock.tree.root)
        assistant = next(
            node
            for node in nodes
            if node.node_type == "assistant"
            and node.payload.get("raw_text") == "final answer"
        )

        assert assistant.payload["thinking_text"] == "checking context"
        assert assistant.payload["raw_text"] == "final answer"
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
async def test_direct_error_event_clears_all_active_turn_statuses(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TurnStarted(text="hello"))
        await bus.emit(ErrorAppended(message="previous error"))
        await bus.emit(StatusUpdated(
            status_id="loop:waiting",
            label="Looping",
            detail="9999999999",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="turn:analyzing",
            label="Analyzing",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="compaction",
            label="Compacting",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="llm:retry",
            label="Retrying",
            detail="retrying in 2s: peer closed connection",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="agent:-1:progress",
            label="Agent step 1/2",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="generic:status",
            label="Generic status",
        ))
        await bus.emit(PermissionPromptShown(
            prompt="Allow tool use?",
            choices=[("y", "Yes", ""), ("n", "No", "")],
            tools=[PermissionToolDetail(name="bash")],
        ))
        await bus.drain()

        assert bus.emit_direct(ErrorAppended(message="top-level failure"))

        for status_id in (
            "turn:analyzing",
            "compaction",
            "llm:retry",
            "agent:-1:progress",
            "generic:status",
            "permission:request",
        ):
            assert isolated_dock.status_record(status_id) is None, status_id
        assert isolated_dock.status_record("loop:waiting") is not None
        assert isolated_dock.status_record("error:current").detail == "top-level failure"

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "previous error" in rendered
        assert "top-level failure" in rendered
        assert "Generic status" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_direct_error_event_preserves_subagent_error_parent(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(SubagentStarted(
            agent_id=7,
            subagent_id="sub-7",
            name="reviewer",
        ))
        await bus.drain()

        subagent = next(
            node
            for node in isolated_dock.tree._all.values()
            if node.node_type == "subagent" and node.agent_run_id == "sub-7"
        )
        assert bus.emit_direct(ErrorAppended(
            agent_id=7,
            message="child failed",
        ))

        error = next(
            node
            for node in isolated_dock.tree._all.values()
            if node.node_type == "error" and node.payload.get("raw_text") == "child failed"
        )
        assert error.parent is subagent
    finally:
        await bus.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_event, expected_error",
    [
        (TurnCompleted(), None),
        (TurnCancelled(), None),
        (TurnFailed(message="provider failed"), "provider failed"),
    ],
    ids=["completed", "cancelled", "failed"],
)
async def test_turn_terminal_event_clears_all_active_turn_statuses(
    isolated_dock,
    terminal_event,
    expected_error,
):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TurnStarted(text="hello"))
        await bus.emit(ErrorAppended(message="previous error"))
        await bus.emit(StatusUpdated(
            status_id="loop:waiting",
            label="Looping",
            detail="9999999999",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="turn:analyzing",
            label="Analyzing",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="compaction",
            label="Compacting",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="llm:retry",
            label="Retrying",
            detail="retrying in 2s: peer closed connection",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="agent:-1:progress",
            label="Agent step 1/2",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="tool-heartbeat:call-1",
            label="Running tool",
            display="record_only",
        ))
        await bus.emit(StatusUpdated(
            status_id="generic:status",
            label="Generic status",
        ))
        await bus.emit(ContextPressureUpdated(
            pressure_id="pressure:hard",
            level="hard",
            outcome="hint_injected",
            reason="context is full",
            turn_count=1,
            pre_tokens=100,
            soft_threshold=80,
            hard_threshold=90,
        ))
        await bus.emit(PermissionPromptShown(
            prompt="Allow tool use?",
            choices=[("y", "Yes", ""), ("n", "No", "")],
            tools=[PermissionToolDetail(name="bash")],
        ))
        await bus.emit(ToolStarted(
            agent_id=7,
            tool_call_id="child-call-1",
            tool_name="bash",
            label="Running child tool",
        ))
        await bus.drain()

        await bus.emit(terminal_event)
        await bus.drain()

        for status_id in (
            "permission:request",
            "turn:analyzing",
            "compaction",
            "llm:retry",
            "agent:-1:progress",
            "agent:7:progress",
            "tool-heartbeat:call-1",
            "generic:status",
            "pressure:hard",
        ):
            assert isolated_dock.status_record(status_id) is None, status_id
        assert isolated_dock.status_record("loop:waiting") is not None
        error_record = isolated_dock.status_record("error:current")
        if expected_error is None:
            assert error_record is None
        else:
            assert error_record is not None
            assert error_record.detail == expected_error

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "previous error" in rendered
        assert "Running child tool" not in rendered
        assert "Generic status" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_error_event_records_error_current_status(isolated_dock):
    """ErrorAppended must record error:current so active_error_text() is non-empty."""
    from voidx.presentation.output.dock import active_error_text, active_error_detail_text

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



@pytest.mark.asyncio
async def test_status_finished_without_prior_update_does_not_log_orphan(isolated_dock, monkeypatch):
    """Defensive StatusFinished for a status that was never created must not log ui_status_orphan."""
    orphan_calls: list[dict] = []

    def fake_log_tool_event(event, *, tool_name="", message="", **kwargs):
        orphan_calls.append({"event": event, "tool_name": tool_name, "message": message})

    monkeypatch.setattr("voidx.observability.tool_log.log_tool_event", fake_log_tool_event)

    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StatusFinished(status_id="tool-heartbeat:call_never_existed"))
        await bus.drain()
    finally:
        await bus.stop()

    orphan_logs = [c for c in orphan_calls if c["event"] == "ui_status_orphan"]
    assert not orphan_logs, f"Expected no orphan log, got: {orphan_logs}"


@pytest.mark.asyncio
async def test_status_finished_for_existing_record_only_status_no_orphan(isolated_dock, monkeypatch):
    """StatusFinished for a record_only status that exists must clear it without orphan log."""
    orphan_calls: list[dict] = []

    def fake_log_tool_event(event, *, tool_name="", message="", **kwargs):
        orphan_calls.append({"event": event, "tool_name": tool_name, "message": message})

    monkeypatch.setattr("voidx.observability.tool_log.log_tool_event", fake_log_tool_event)

    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(StatusUpdated(
            status_id="tool-heartbeat:call_abc",
            label="Tool running",
            detail="read still running (1s elapsed)",
            stage="working",
            display="record_only",
        ))
        await bus.drain()
        assert isolated_dock.status_record("tool-heartbeat:call_abc") is not None

        await bus.emit(StatusFinished(status_id="tool-heartbeat:call_abc"))
        await bus.drain()
        assert isolated_dock.status_record("tool-heartbeat:call_abc") is None
    finally:
        await bus.stop()

    orphan_logs = [c for c in orphan_calls if c["event"] == "ui_status_orphan"]
    assert not orphan_logs, f"Expected no orphan log, got: {orphan_logs}"


@pytest.mark.asyncio
async def test_context_pressure_events_stay_internal(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(ContextPressureUpdated(
            pressure_id="context-pressure:turn-1",
            level="soft",
            outcome="hint_injected",
            reason="soft_threshold",
            turn_count=1,
            pre_tokens=80_000,
            soft_threshold=75_000,
            hard_threshold=90_000,
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Context pressure" not in rendered
        assert "soft_threshold" not in rendered
        record = isolated_dock.status_record("context-pressure:turn-1")
        assert record is not None
        assert record.detail == "soft_threshold"

        await bus.emit(ContextPressureFinished(
            pressure_id="context-pressure:turn-1",
            level="soft",
            outcome="turn_converged",
        ))
        await bus.drain()

        rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
        assert "Context pressure" not in rendered
        assert isolated_dock.status_record("context-pressure:turn-1") is None
    finally:
        await bus.stop()
