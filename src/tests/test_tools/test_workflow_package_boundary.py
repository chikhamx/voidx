from voidx.tools.workflow import WorkflowInput, WorkflowTool
from voidx.tools.workflow import __all__ as workflow_exports
from voidx.tools.workflow.state import _current_runs
from voidx.tools.base import ToolContext
from voidx.workflow.types import WorkflowRunStatus


def test_workflow_package_exports_only_public_tool_api():
    assert workflow_exports == ["WorkflowInput", "WorkflowTool"]
    assert WorkflowInput.__module__ == "voidx.tools.workflow"
    assert WorkflowTool.__module__ == "voidx.tools.workflow"


def test_workflow_state_falls_back_to_legacy_active_names(tmp_path):
    ctx = ToolContext(
        workspace=str(tmp_path),
        workflow_runs=[],
        active_workflow_names=[" DEBUG ", "debug", "tdd"],
    )

    runs = _current_runs(ctx)

    assert [(run.name, run.status) for run in runs] == [
        ("debug", WorkflowRunStatus.ACTIVE),
        ("tdd", WorkflowRunStatus.ACTIVE),
    ]


def test_workflow_submodules_import_without_package_helper_exports():
    import voidx.tools.workflow.actions as actions
    import voidx.tools.workflow.queries as queries
    import voidx.tools.workflow.result as result
    import voidx.tools.workflow.state as state

    assert actions._enter
    assert queries._match_node
    assert result._success
    assert state._active_runs
