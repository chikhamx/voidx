from voidx.agent.application.subagent_status import (
    activity_recommendation,
    public_child_run_snapshot,
    render_child_activity,
    render_child_progress,
    render_child_run_metrics,
)
from voidx.agent.domain.subagent import (
    AgentActivity,
    AgentProgress,
    AgentRun,
    AgentToolActivity,
)


def _run(**updates) -> AgentRun:
    values = {
        "run_id": "run_status",
        "session_id": "session-status",
        "parent_run_id": "root:session-status",
        "agent_type": "sub",
        "agent_name": "voidx",
        "description": "Goal: inspect status",
        "status": "running",
        "created_at": 100.0,
        "updated_at": 188.0,
        "progress": AgentProgress(
            files_read=7,
            files_edited=3,
            commands_run=4,
            searches=6,
            other_actions=2,
        ),
        "current_activity": AgentActivity(
            category="searching",
            status="running",
            started_at=180.0,
            last_observed_at=188.0,
        ),
        "recent_activity": AgentActivity(
            category="editing",
            status="succeeded",
            started_at=160.0,
            last_observed_at=179.0,
            finished_at=179.0,
        ),
        "last_activity_at": 188.0,
    }
    values.update(updates)
    return AgentRun(**values)


def test_render_child_progress_uses_abstract_categories_in_fixed_order():
    assert render_child_progress(_run().progress) == (
        "read 7 files · edited 3 files · ran 4 commands · "
        "searched 6 times · 2 other actions"
    )
    assert render_child_progress(AgentProgress(files_read=1, commands_run=1)) == (
        "read 1 file · ran 1 command"
    )
    assert render_child_progress(AgentProgress()) == ""


def test_render_child_activity_uses_abstract_current_and_recent_categories():
    assert render_child_activity(_run(), sampled_at=200.0) == [
        "Current: searching · activity 12s ago",
        "Recent: editing · succeeded 21s ago",
    ]
    assert render_child_run_metrics(_run(), sampled_at=200.0) == (
        "elapsed 1m40s · current: searching · activity 12s ago"
    )


def test_public_child_run_snapshot_excludes_concrete_tool_details():
    run = _run(
        active_tools=[
            AgentToolActivity(
                tool_name="secret_search_tool",
                tool_call_id="call-secret",
                status="running",
                started_at=180.0,
            )
        ],
        last_tool=AgentToolActivity(
            tool_name="secret_replace_tool",
            tool_call_id="call-replace",
            status="succeeded",
            started_at=150.0,
            finished_at=160.0,
        ),
        wait_outcome="timed_out",
    )

    snapshot = public_child_run_snapshot(run)

    assert snapshot["progress"] == {
        "files_read": 7,
        "files_edited": 3,
        "commands_run": 4,
        "searches": 6,
        "other_actions": 2,
    }
    assert snapshot["current_activity"]["category"] == "searching"
    assert snapshot["recent_activity"]["category"] == "editing"
    assert "active_tools" not in snapshot
    assert "last_tool" not in snapshot
    assert "wait_outcome" not in snapshot
    assert "secret_search_tool" not in str(snapshot)
    assert "secret_replace_tool" not in str(snapshot)
    assert "call-secret" not in str(snapshot)


def test_activity_recommendation_uses_this_wait_start_boundary():
    assert activity_recommendation(_run(last_activity_at=188.0), wait_started_at=180.0) == "wait"
    assert activity_recommendation(_run(last_activity_at=180.0), wait_started_at=180.0) == "wait"
    assert activity_recommendation(_run(last_activity_at=179.999), wait_started_at=180.0) == "cancel"
    assert activity_recommendation(_run(last_activity_at=None), wait_started_at=180.0) == "cancel"
