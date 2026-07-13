from __future__ import annotations

from voidx.tools.base import ToolContext


class FakeLoopManager:
    pass


def test_tool_context_keeps_loop_manager_and_shared_state_references() -> None:
    loop_manager = FakeLoopManager()
    file_mtimes: dict[str, dict[str, int]] = {}
    file_read_coverage: dict[str, dict] = {}
    workflow_repeat_tracker: dict[str, dict[str, int]] = {}

    ctx = ToolContext(
        workspace="/tmp/workspace",
        loop_manager=loop_manager,
        file_mtimes=file_mtimes,
        file_read_coverage=file_read_coverage,
        workflow_repeat_tracker=workflow_repeat_tracker,
    )

    assert ctx.loop_manager is loop_manager
    assert ctx.file_mtimes is file_mtimes
    assert ctx.file_read_coverage is file_read_coverage
    assert ctx.workflow_repeat_tracker is workflow_repeat_tracker


def test_tool_context_loop_manager_is_excluded_from_serialization() -> None:
    loop_manager = FakeLoopManager()
    ctx = ToolContext(workspace="/tmp/workspace", loop_manager=loop_manager)

    assert "loop_manager" not in ctx.model_dump()
