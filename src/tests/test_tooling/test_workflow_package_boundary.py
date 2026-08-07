from voidx.agent.adapters.tools.automation.workflow import WorkflowInput, WorkflowTool
from voidx.agent.adapters.tools.automation.workflow import __all__ as workflow_exports
from voidx.agent.adapters.tools.automation.workflow_state import _current_runs
from tests.agent_tool_context import agent_tool_context as ToolContext
from voidx.agent.domain.automation.workflow import WorkflowRunStatus


def test_workflow_package_exports_only_public_tool_api():
    assert workflow_exports == ["WorkflowInput", "WorkflowTool"]
    assert WorkflowInput.__module__ == "voidx.agent.adapters.tools.automation.workflow"
    assert WorkflowTool.__module__ == "voidx.agent.adapters.tools.automation.workflow"


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
    import voidx.agent.adapters.tools.automation.workflow_actions as actions
    import voidx.agent.adapters.tools.automation.workflow_queries as queries
    import voidx.agent.adapters.tools.automation.workflow_result as result
    import voidx.agent.adapters.tools.automation.workflow_state as state

    assert actions._enter
    assert queries._match_node
    assert result._success
    assert state._active_runs
