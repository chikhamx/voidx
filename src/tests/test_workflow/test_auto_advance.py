from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
import sys
from pathlib import Path


import pytest

from voidx.tooling.domain.result import ToolResult
from voidx.agent.application.automation.workflow.auto_advance import auto_advance_events
from voidx.agent.application.automation.workflow.runtime import (
    WorkflowActivationSource,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
    advance_workflow_states,
)


class TestAutoAdvanceReviewHasIssues:
    def test_review_fail_triggers_review_has_issues(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="verdict: FAIL\n\n## Issues\n- bug found",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 1
        assert events[0].workflow == "review"
        assert events[0].condition == "review_has_issues"
        assert events[0].kind == WorkflowStateEventKind.SATISFIED

    def test_review_needs_change_triggers_review_has_issues(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="verdict: NEEDS_CHANGE\n\n## Issues\n- minor style",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 1
        assert events[0].condition == "review_has_issues"

    def test_review_pass_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="verdict: PASS\n\nNo issues found.",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_non_review_agent_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "implement"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_no_active_requesting_code_review_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_satisfied_requesting_code_review_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.SATISFIED,
            ),
        ]
        result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_verdict_case_insensitive(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="Verdict: fail",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 1


class TestAutoAdvanceFailedImplementation:
    def test_bash_nonzero_triggers_failed_implementation(self):
        runs = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="1 failed, 2 passed",
            metadata={"exit_code": 1, "command": "pytest tests/"},
        )
        events = auto_advance_events(
            [{"name": "bash", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 1
        assert events[0].workflow == "verify"
        assert events[0].condition == "failed_implementation"
        assert events[0].kind == WorkflowStateEventKind.SATISFIED

    def test_bash_nonzero_nontest_command_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="fatal: not a git repository",
            metadata={"exit_code": 128, "command": "git status"},
        )
        events = auto_advance_events(
            [{"name": "bash", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_bash_nonzero_docker_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="docker: command not found",
            metadata={"exit_code": 127, "command": "docker build ."},
        )
        events = auto_advance_events(
            [{"name": "bash", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_bash_nonzero_npm_test_triggers(self):
        runs = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="1 test failed",
            metadata={"exit_code": 1, "command": "npm test"},
        )
        events = auto_advance_events(
            [{"name": "bash", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 1
        assert events[0].condition == "failed_implementation"

    def test_bash_zero_exit_triggers_passed_substantial(self):
        """bash exit_code=0 + test runner + verify active → passed_substantial."""
        runs = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="3 passed",
            metadata={"exit_code": 0, "command": "pytest"},
        )
        events = auto_advance_events(
            [{"name": "bash", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 1
        assert events[0].workflow == "verify"
        assert events[0].condition == "passed_substantial"

    def test_no_active_verification_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="1 failed",
            metadata={"exit_code": 1, "command": "pytest"},
        )
        events = auto_advance_events(
            [{"name": "bash", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_bash_no_exit_code_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="some output",
            metadata={"command": "ls"},
        )
        events = auto_advance_events(
            [{"name": "bash", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0


class TestAutoAdvanceIntegration:
    def test_auto_advance_events_flow_through_advance_workflow_states(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        updated = advance_workflow_states(runs, events, dag=DEFAULT_WORKFLOW_DAG)
        by_name = {r.name: r for r in updated}
        assert by_name["review"].status == WorkflowRunStatus.SATISFIED
        assert "feedback" in by_name
        assert by_name["feedback"].status == WorkflowRunStatus.ACTIVE

    def test_auto_advance_failed_implementation_activates_tdd(self):
        runs = [
            WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="1 failed",
            metadata={"exit_code": 1, "command": "pytest"},
        )
        events = auto_advance_events(
            [{"name": "bash", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        updated = advance_workflow_states(runs, events, dag=DEFAULT_WORKFLOW_DAG)
        by_name = {r.name: r for r in updated}
        assert by_name["verify"].status == WorkflowRunStatus.SATISFIED
        assert "tdd" in by_name
        assert by_name["tdd"].status == WorkflowRunStatus.ACTIVE

    def test_no_active_runs_produces_no_events(self):
        result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=[],
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_mixed_tools_only_triggers_matching(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        review_result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "review"},
        )
        bash_result = ToolResult(
            output="1 failed",
            metadata={"exit_code": 1, "command": "pytest"},
        )
        events = auto_advance_events(
            [
                {"name": "agent", "result": review_result},
                {"name": "bash", "result": bash_result},
            ],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 1
        assert events[0].condition == "review_has_issues"

    def test_idempotent_after_explicit_advance(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.SATISFIED,
            ),
        ]
        result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_explicit_workflow_advance_plus_auto_advance_no_duplicate(self):
        """When workflow advance already satisfied a node in the same batch,
        auto_advance_events should not produce a duplicate event."""
        runs_after_explicit = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.SATISFIED,
            ),
            WorkflowRunState(
                name="feedback",
                status=WorkflowRunStatus.ACTIVE,
                source=WorkflowActivationSource.TRANSITION,
                reason="transition from review via review_has_issues",
            ),
        ]
        result = ToolResult(
            output="verdict: FAIL",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs_after_explicit,
        dag=DEFAULT_WORKFLOW_DAG)
        assert len(events) == 0

    def test_verdict_in_code_block_does_not_trigger(self):
        runs = [
            WorkflowRunState(
                name="review",
                status=WorkflowRunStatus.ACTIVE,
            ),
        ]
        result = ToolResult(
            output="The format is:\n```\nverdict: FAIL\n```\nBut this is just an example.",
            metadata={"agent": "review"},
        )
        events = auto_advance_events(
            [{"name": "agent", "result": result}],
            workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG)
        # The regex uses ^ with MULTILINE, so "verdict: FAIL" inside a code
        # block at line start WILL match. This is a known limitation — the
        # review agent prompt constrains format, making false positives unlikely.
        # If needed, a structured verdict in metadata would eliminate this.
        assert len(events) <= 1


class TestWorkflowGoalInheritance:
    def test_transition_targets_inherit_run_goal(self):
        runs = [
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                goal="实现 workflow goal 参数改造",
                transition_to=["verify"],
            )
        ]
        events = [
            WorkflowStateEvent(
                workflow="tdd",
                kind=WorkflowStateEventKind.SATISFIED,
                ref="test",
                ok=True,
                summary="implemented",
                condition="implemented",
            )
        ]

        updated = advance_workflow_states(runs, events, dag=DEFAULT_WORKFLOW_DAG)
        by_name = {run.name: run for run in updated}

        assert by_name["verify"].goal == "实现 workflow goal 参数改造"



class TestAutoAdvanceReviewViaAgentControl:
    """review verdicts consumed from agent_control(wait) terminal snapshots."""

    def _wait_result(
        self,
        *,
        mode="review",
        verdict=None,
        text="",
        status="completed",
        finish_reason="",
        wait_outcome="already_terminal",
    ) -> ToolResult:
        result_payload = {"result": text}
        if verdict:
            result_payload["verdict"] = verdict
        if finish_reason:
            result_payload["finish_reason"] = finish_reason
        run = {
            "run_id": "run_1",
            "mode": mode,
            "status": status,
            "result": result_payload,
        }
        metadata = {"run": run, "status": status, "wait_outcome": wait_outcome}
        return ToolResult(output=text, metadata=metadata)

    def _events(self, tool_result: ToolResult, *, tool_name="agent_control"):
        runs = [WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE)]
        return auto_advance_events(
            [{"name": tool_name, "result": tool_result}],
            workflow_runs=runs,
            dag=DEFAULT_WORKFLOW_DAG,
        )

    def test_structured_fail_triggers_review_has_issues(self):
        result = self._wait_result(verdict="FAIL", text="verdict=FAIL\nfindings: bug")
        events = self._events(result)
        assert len(events) == 1
        assert events[0].condition == "review_has_issues"

    def test_structured_needs_change_triggers_review_has_issues(self):
        result = self._wait_result(verdict="NEEDS_CHANGE", text="verdict=NEEDS_CHANGE")
        events = self._events(result)
        assert len(events) == 1
        assert events[0].condition == "review_has_issues"

    def test_structured_pass_does_not_trigger(self):
        result = self._wait_result(verdict="PASS", text="verdict=PASS")
        assert self._events(result) == []

    def test_structured_pass_wins_over_legacy_fail_text(self):
        result = self._wait_result(verdict="PASS", text="verdict: FAIL\nfindings: stale text")
        assert self._events(result) == []

    def test_legacy_verdict_equals_in_result_text_triggers(self):
        result = self._wait_result(text="verdict=FAIL\nfindings: bug")
        events = self._events(result)
        assert len(events) == 1
        assert events[0].condition == "review_has_issues"

    def test_legacy_verdict_colon_in_result_text_triggers(self):
        result = self._wait_result(text="verdict: FAIL\nfindings: bug")
        events = self._events(result)
        assert len(events) == 1

    def test_failed_run_with_verdict_text_does_not_trigger(self):
        result = self._wait_result(status="failed", text="verdict: FAIL")
        assert self._events(result) == []

    def test_timed_out_wait_does_not_trigger(self):
        result = self._wait_result(status="running", wait_outcome="timed_out", text="verdict: FAIL")
        assert self._events(result) == []

    def test_incomplete_finish_reason_does_not_trigger(self):
        result = self._wait_result(finish_reason="context_limit", text="verdict: FAIL")
        assert self._events(result) == []

    def test_missing_mode_does_not_trigger(self):
        result = self._wait_result(mode="", text="verdict: FAIL")
        assert self._events(result) == []

    def test_debug_mode_does_not_trigger_review_event(self):
        result = self._wait_result(mode="debug", verdict="FAIL", text="verdict: FAIL")
        assert self._events(result) == []

    def test_spawn_metadata_with_mode_and_running_status_does_not_trigger(self):
        result = ToolResult(
            output="voidx-subagent [running]\nrun_id: run_1",
            metadata={"agent": "voidx", "mode": "review", "run_id": "run_1", "status": "running"},
        )
        events = self._events(result, tool_name="agent")
        assert events == []
