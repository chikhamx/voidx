from __future__ import annotations

from pydantic import Field, SkipValidation

from voidx.agent.adapters.tools.context import AgentToolExecutionContext, AgentToolRuntime
from voidx.tooling.domain.file_tracking import FileStateStore


class ToolContext(AgentToolExecutionContext):
    file_state: SkipValidation[FileStateStore] = Field(default_factory=FileStateStore)


def test_tool_context_keeps_shared_state_references() -> None:
    file_mtimes: dict[str, dict[str, int]] = {}
    file_read_coverage: dict[str, dict] = {}
    workflow_repeat_tracker: dict[str, dict[str, int]] = {}

    ctx = ToolContext(
        workspace="/tmp/workspace",
        file_state=FileStateStore(
            mtimes=file_mtimes,
            read_coverage=file_read_coverage,
        ),
        runtime=AgentToolRuntime(workflow_repeat_state=workflow_repeat_tracker),
    )

    assert ctx.file_state.mtimes is file_mtimes
    assert ctx.file_state.read_coverage is file_read_coverage
    assert ctx.runtime.workflow_repeat_state is workflow_repeat_tracker


def test_tool_context_agent_group_is_excluded_from_serialization() -> None:
    ctx = ToolContext(workspace="/tmp/workspace", runtime=AgentToolRuntime(loop_control=object()))

    assert "runtime" not in ctx.model_dump()
