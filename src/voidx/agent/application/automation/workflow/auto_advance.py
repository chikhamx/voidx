"""Auto-advance workflow nodes based on tool execution signals.

Detects structured signals from tool results and automatically produces
WorkflowStateEvent entries to drive DAG transitions, so the LLM does not
need to call workflow for well-defined conditions:

- review_has_issues: review agent returns FAIL or NEEDS_CHANGE
- failed_implementation: bash/test execution fails while verify is active
- passed_substantial: bash/test execution passes while verify is active

failed_bug is NOT auto-detected: distinguishing "original bug still present"
from "implementation broke something" requires semantic analysis that only
the LLM can provide via explicit workflow.
"""

from __future__ import annotations

import re

from voidx.agent.domain.automation.workflow_schema import WorkflowDAG
from voidx.agent.domain.automation.workflow import (
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)

_REVIEW_VERDICT_RE = re.compile(
    r"^verdict\s*:\s*(FAIL|NEEDS_CHANGE)", re.IGNORECASE | re.MULTILINE
)

_TEST_COMMAND_RE = re.compile(
    r"\b(pytest|unittest|nosetests|trial|cargo test|go test|npm test|yarn test|pnpm test|"
    r"mvn test|gradle test|bazel test|dotnet test|mix test|jest|vitest|mocha)\b",
    re.IGNORECASE,
)


def auto_advance_events(
    executed_tools: list[dict],
    *,
    workflow_runs: list[WorkflowRunState],
    dag: WorkflowDAG,
) -> list[WorkflowStateEvent]:
    """Inspect executed tool results and return auto-advance events.

    Parameters
    ----------
    executed_tools
        List of dicts with keys: name (tool id), result (ToolResult or similar).
    workflow_runs
        Current workflow run states.

    Returns
    -------
    List of WorkflowStateEvent for conditions that were auto-detected.
    Only produces events for active nodes that have matching outgoing edges.
    """
    active_names = {
        run.name.strip().lower()
        for run in workflow_runs
        if run.status == WorkflowRunStatus.ACTIVE and run.name.strip()
    }
    if not active_names:
        return []

    events: list[WorkflowStateEvent] = []

    for item in executed_tools:
        tool_name = item.get("name", "")
        result = item.get("result")
        if result is None:
            continue

        metadata = getattr(result, "metadata", None) or {}
        output = getattr(result, "output", "") or ""

        if tool_name == "agent":
            event = _check_review_result(output, metadata, active_names, dag)
            if event:
                events.append(event)
        elif tool_name in ("bash", "powershell"):
            events.extend(_check_shell_result(metadata, active_names, dag))
            verify_event = _check_verify_passed(metadata, active_names, dag)
            if verify_event:
                events.append(verify_event)

    return events


def _check_review_result(
    output: str,
    metadata: dict,
    active_names: set[str],
    dag: WorkflowDAG,
) -> WorkflowStateEvent | None:
    """Detect review_has_issues when a review agent returns FAIL/NEEDS_CHANGE.

    Relies on the review agent following the `verdict: PASS|FAIL|NEEDS_CHANGE`
    format specified in its system prompt. The regex matches at line start to
    avoid false positives from inline mentions.
    """
    if metadata.get("agent") != "review":
        return None
    if "review" not in active_names:
        return None
    if not _REVIEW_VERDICT_RE.search(output):
        return None

    edges = dag.edges_from("review")
    if not any(e.condition == "review_has_issues" for e in edges):
        return None

    return WorkflowStateEvent(
        workflow="review",
        kind=WorkflowStateEventKind.SATISFIED,
        ref="auto:review_has_issues",
        ok=False,
        summary="Review returned issues (FAIL or NEEDS_CHANGE).",
        reason="auto-detected from review agent verdict",
        condition="review_has_issues",
    )


def _check_shell_result(
    metadata: dict,
    active_names: set[str],
    dag: WorkflowDAG,
) -> list[WorkflowStateEvent]:
    """Detect failed_implementation from shell (bash/powershell) test failures.

    A non-zero exit code from a test/verification command while
    verify is active is treated as a failed_implementation
    signal. Only commands matching known test runners trigger auto-advance;
    arbitrary bash failures (git, ls, docker, etc.) are ignored.

    failed_bug is NOT auto-detected from exit codes alone: distinguishing
    "original bug still present" from "implementation broke something"
    requires semantic analysis that only the LLM can provide.
    """
    exit_code = metadata.get("exit_code")
    if exit_code is None:
        return []
    try:
        if int(exit_code) == 0:
            return []
    except (TypeError, ValueError):
        return []

    command = metadata.get("command", "")
    if not _TEST_COMMAND_RE.search(command):
        return []

    if "verify" not in active_names:
        return []

    edges = dag.edges_from("verify")
    if not any(e.condition == "failed_implementation" for e in edges):
        return []

    return [WorkflowStateEvent(
        workflow="verify",
        kind=WorkflowStateEventKind.SATISFIED,
        ref="auto:failed_implementation",
        ok=False,
        summary="Verification failed — implementation issue detected.",
        reason="auto-detected from non-zero test command exit code",
        condition="failed_implementation",
    )]


def _check_verify_passed(
    metadata: dict,
    active_names: set[str],
    dag: WorkflowDAG,
) -> WorkflowStateEvent | None:
    """Detect passed_substantial when a test command exits 0 while verify is active."""
    exit_code = metadata.get("exit_code")
    if exit_code is None:
        return None
    try:
        if int(exit_code) != 0:
            return None
    except (TypeError, ValueError):
        return None

    command = metadata.get("command", "")
    if not _TEST_COMMAND_RE.search(command):
        return None

    if "verify" not in active_names:
        return None

    edges = dag.edges_from("verify")
    if not any(e.condition == "passed_substantial" for e in edges):
        return None

    return WorkflowStateEvent(
        workflow="verify",
        kind=WorkflowStateEventKind.SATISFIED,
        ref="auto:passed_substantial",
        ok=True,
        summary="Verification passed — changes ready for review.",
        reason="auto-detected from zero test command exit code",
        condition="passed_substantial",
    )
