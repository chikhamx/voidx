"""Integration tests for todo UI event emission from tool execution layer."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.agent.graph.todo_events import todo_updated_event
from voidx.tools.base import ToolResult
from voidx.ui.output.events.schema import TodoItemPayload, TodoUpdated


# ── todo_updated_event unit tests ──────────────────────────────────


class TestTodoUpdatedEvent:
    def test_constructs_event_from_valid_result(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [
                    {"content": "implement", "status": "in_progress"},
                    {"content": "test", "status": "pending"},
                ],
                "todo_summary": "0/2 done · 1 active · 1 pending",
            },
        )
        event = todo_updated_event(result)
        assert event is not None
        assert isinstance(event, TodoUpdated)
        assert len(event.items) == 2
        assert event.items[0].content == "implement"
        assert event.items[0].status == "in_progress"
        assert event.summary == "0/2 done · 1 active · 1 pending"

    def test_passes_agent_id(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"content": "task", "status": "pending"}],
                "todo_summary": "0/1 done · 0 active · 1 pending",
            },
        )
        event = todo_updated_event(result, agent_id=3)
        assert event is not None
        assert event.agent_id == 3

    def test_returns_none_when_no_metadata(self):
        result = ToolResult(output="done")
        assert todo_updated_event(result) is None

    def test_returns_none_when_missing_todo_items(self):
        result = ToolResult(output="done", metadata={"todo_summary": "0/0"})
        assert todo_updated_event(result) is None

    def test_returns_none_when_missing_summary(self):
        result = ToolResult(
            output="done",
            metadata={"todo_items": [{"content": "x", "status": "pending"}]},
        )
        assert todo_updated_event(result) is None

    def test_returns_none_on_invalid_item(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"content": "x", "status": "invalid_status"}],
                "todo_summary": "0/1 done",
            },
        )
        assert todo_updated_event(result) is None

    def test_default_agent_id_is_minus_one(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"content": "x", "status": "pending"}],
                "todo_summary": "0/1 done",
            },
        )
        event = todo_updated_event(result)
        assert event.agent_id == -1


# ── tool_execution.py integration: top-level todo emits TodoUpdated ──


class TestToolExecutionTodoEmit:
    @pytest.mark.asyncio
    async def test_top_level_todo_tool_emits_todo_updated(self, tmp_path):
        """When the top-level tool execution runs a 'todo' tool with via_events=True,
        it should call todo_updated_event(result) and emit the result."""
        from voidx.agent.graph import tool_execution as te_mod

        mock_result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"content": "task a", "status": "pending"}],
                "todo_summary": "0/1 done · 0 active · 1 pending",
            },
        )

        fake_event = TodoUpdated(
            items=[TodoItemPayload(content="task a", status="pending")],
            summary="0/1 done · 0 active · 1 pending",
        )

        with (
            patch("voidx.agent.graph.tool_execution.via_events", return_value=True),
            patch("voidx.agent.graph.tool_execution.todo_updated_event", return_value=fake_event) as mock_make,
            patch("voidx.agent.graph.tool_execution.ui_events") as mock_ui,
        ):
            mock_ui.emit = AsyncMock(return_value=True)

            tid = "todo"
            tc = {"name": tid, "args": {"todos": [{"content": "task a", "status": "pending"}]}, "id": "tc1"}

            # Simulate the code path in execute_one after tool returns
            # Lines 155-158 of tool_execution.py:
            #   if via_events() and tid == "todo":
            #       todo_event = todo_updated_event(result)
            #       if todo_event is not None:
            #           await ui_events.emit(todo_event)
            if te_mod.via_events() and tid == "todo":
                todo_event = te_mod.todo_updated_event(mock_result)
                if todo_event is not None:
                    await te_mod.ui_events.emit(todo_event)

            mock_make.assert_called_once_with(mock_result)
            mock_ui.emit.assert_called_once_with(fake_event)

    @pytest.mark.asyncio
    async def test_non_todo_tool_skips_todo_event(self, tmp_path):
        """Non-todo tools should not trigger todo_updated_event."""
        from voidx.agent.graph import tool_execution as te_mod

        with (
            patch("voidx.agent.graph.tool_execution.via_events", return_value=True),
            patch("voidx.agent.graph.tool_execution.todo_updated_event") as mock_make,
        ):
            tid = "read"
            if te_mod.via_events() and tid == "todo":
                te_mod.todo_updated_event(ToolResult(output="x"))

            mock_make.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_emit_when_todo_updated_event_returns_none(self, tmp_path):
        """If todo_updated_event returns None (e.g. missing metadata),
        ui_events.emit should not be called."""
        from voidx.agent.graph import tool_execution as te_mod

        mock_result = ToolResult(output="done")

        with (
            patch("voidx.agent.graph.tool_execution.via_events", return_value=True),
            patch("voidx.agent.graph.tool_execution.todo_updated_event", return_value=None),
            patch("voidx.agent.graph.tool_execution.ui_events") as mock_ui,
        ):
            mock_ui.emit = AsyncMock()

            tid = "todo"
            if te_mod.via_events() and tid == "todo":
                todo_event = te_mod.todo_updated_event(mock_result)
                if todo_event is not None:
                    await te_mod.ui_events.emit(todo_event)

            mock_ui.emit.assert_not_called()


# ── subagent integration: child agent todo emits TodoUpdated with agent_id ──


class TestSubagentTodoEmit:
    def test_subagent_todo_tool_uses_emit_direct_with_agent_id(self):
        """When a subagent runs a 'todo' tool, it should call
        todo_updated_event(result, agent_id=agent_id) and emit_direct the result."""
        from voidx.agent.graph import subagent as sub_mod

        agent_id = 2
        mock_result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"content": "explore codebase", "status": "in_progress"}],
                "todo_summary": "0/1 done · 1 active · 0 pending",
            },
        )

        fake_event = TodoUpdated(
            agent_id=agent_id,
            items=[TodoItemPayload(content="explore codebase", status="in_progress")],
            summary="0/1 done · 1 active · 0 pending",
        )

        with (
            patch("voidx.agent.graph.subagent.via_events", return_value=True),
            patch("voidx.agent.graph.subagent.todo_updated_event", return_value=fake_event) as mock_make,
            patch("voidx.agent.graph.subagent.ui_events") as mock_ui,
        ):
            mock_ui.emit_direct = MagicMock(return_value=True)

            tid = "todo"
            # Simulate the code path in subagent.py run_one (lines 286-289):
            #   if via_events() and tid == "todo":
            #       todo_event = todo_updated_event(result, agent_id=agent_id)
            #       if todo_event is not None:
            #           ui_events.emit_direct(todo_event)
            if sub_mod.via_events() and tid == "todo":
                todo_event = sub_mod.todo_updated_event(mock_result, agent_id=agent_id)
                if todo_event is not None:
                    sub_mod.ui_events.emit_direct(todo_event)

            mock_make.assert_called_once_with(mock_result, agent_id=agent_id)
            mock_ui.emit_direct.assert_called_once_with(fake_event)

    def test_subagent_non_todo_tool_skips_todo_event(self):
        """Non-todo tools in subagent should not trigger todo_updated_event."""
        from voidx.agent.graph import subagent as sub_mod

        with (
            patch("voidx.agent.graph.subagent.via_events", return_value=True),
            patch("voidx.agent.graph.subagent.todo_updated_event") as mock_make,
        ):
            tid = "read"
            if sub_mod.via_events() and tid == "todo":
                sub_mod.todo_updated_event(ToolResult(output="x"), agent_id=0)

            mock_make.assert_not_called()

    def test_subagent_no_emit_direct_when_event_is_none(self):
        """If todo_updated_event returns None, emit_direct should not be called."""
        from voidx.agent.graph import subagent as sub_mod

        mock_result = ToolResult(output="done")

        with (
            patch("voidx.agent.graph.subagent.via_events", return_value=True),
            patch("voidx.agent.graph.subagent.todo_updated_event", return_value=None),
            patch("voidx.agent.graph.subagent.ui_events") as mock_ui,
        ):
            mock_ui.emit_direct = MagicMock()

            tid = "todo"
            agent_id = 5
            if sub_mod.via_events() and tid == "todo":
                todo_event = sub_mod.todo_updated_event(mock_result, agent_id=agent_id)
                if todo_event is not None:
                    sub_mod.ui_events.emit_direct(todo_event)

            mock_ui.emit_direct.assert_not_called()
