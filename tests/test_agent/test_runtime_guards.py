import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.graph.runtime_guards import (
    RuntimeGuardState,
    ToolCycleSummary,
    WallClockGuardState,
    build_failure_key,
    cycle_summary_from_tools,
    error_kind_from_result,
    todo_status_signature,
)
from voidx.tools.base import ToolResult


def test_failure_loop_guidance_escalates_and_blocks_same_call():
    guards = RuntimeGuardState()
    call = {"name": "read", "args": {"file_path": "missing.py"}}
    result = ToolResult(
        output="File not found: missing.py",
        metadata={"error": True, "error_kind": "file_not_found"},
    )

    key = build_failure_key(call, result)

    assert guards.tool_failures.record_failure(key, "File not found") is None

    second = guards.tool_failures.record_failure(key, "File not found")
    assert second is not None
    assert second.level == "light"
    assert "failed twice" in second.message
    assert guards.tool_failures.should_block(call) is False

    third = guards.tool_failures.record_failure(key, "File not found")
    assert third is not None
    assert third.level == "stern"
    assert "failed 3 times" in third.message
    assert "Stop retrying it now" in third.message
    assert guards.tool_failures.should_block(call) is True


def test_failure_loop_does_not_block_materially_different_args():
    guards = RuntimeGuardState()
    original = {"name": "grep", "args": {"pattern": "old", "path": "src"}}
    changed = {"name": "grep", "args": {"pattern": "new", "path": "src"}}
    result = ToolResult(output="grep failed", metadata={"error": True, "error_kind": "unknown_error"})
    key = build_failure_key(original, result)

    guards.tool_failures.record_failure(key, "grep failed")
    guards.tool_failures.record_failure(key, "grep failed")
    guards.tool_failures.record_failure(key, "grep failed")

    assert guards.tool_failures.should_block(original) is True
    assert guards.tool_failures.should_block(changed) is False


def test_failure_loop_success_clears_tool_blocks():
    guards = RuntimeGuardState()
    failing = {"name": "read", "args": {"file_path": "missing.py"}}
    recovered = {"name": "read", "args": {"file_path": "present.py"}}
    result = ToolResult(
        output="File not found: missing.py",
        metadata={"error": True, "error_kind": "file_not_found"},
    )

    key = build_failure_key(failing, result)
    guards.tool_failures.record_failure(key, "File not found")
    guards.tool_failures.record_failure(key, "File not found")
    guards.tool_failures.record_failure(key, "File not found")
    assert guards.tool_failures.should_block(failing) is True

    guards.tool_failures.record_success(recovered)
    assert guards.tool_failures.should_block(failing) is False


def test_failure_loop_new_key_clears_old_blocks():
    guards = RuntimeGuardState()
    original = {"name": "read", "args": {"file_path": "missing.py"}}
    changed = {"name": "read", "args": {"file_path": "other.py"}}
    result = ToolResult(
        output="File not found: missing.py",
        metadata={"error": True, "error_kind": "file_not_found"},
    )

    key = build_failure_key(original, result)
    guards.tool_failures.record_failure(key, "File not found")
    guards.tool_failures.record_failure(key, "File not found")
    guards.tool_failures.record_failure(key, "File not found")
    assert guards.tool_failures.should_block(original) is True

    changed_key = build_failure_key(changed, ToolResult(
        output="File not found: other.py",
        metadata={"error": True, "error_kind": "file_not_found"},
    ))
    guards.tool_failures.record_failure(changed_key, "File not found")
    assert guards.tool_failures.should_block(original) is False


def test_todo_status_signature_ignores_text_rewrites_and_tracks_status_progress():
    first = {
        "total": 2,
        "done": 0,
        "in_progress": 0,
        "pending": 2,
        "cancelled": 0,
    }
    rewritten = {
        "total": 2,
        "done": 0,
        "in_progress": 0,
        "pending": 2,
        "cancelled": 0,
    }
    progressed = {
        "total": 2,
        "done": 1,
        "in_progress": 1,
        "pending": 0,
        "cancelled": 0,
    }

    assert todo_status_signature(first) == todo_status_signature(rewritten)
    assert todo_status_signature(first) != todo_status_signature(progressed)


def test_repetitive_todo_cycle_warns_then_skips_then_terminates():
    guards = RuntimeGuardState()
    summary = ToolCycleSummary(
        tool_names=["todo"],
        only_tool="todo",
        call_count=1,
        has_progress=False,
    )

    assert guards.repetitive_tools.record_cycle(summary) is None
    warning = guards.repetitive_tools.record_cycle(summary)
    assert warning is not None
    assert warning.level == "light"
    assert "only called todo" in warning.message

    decision = guards.repetitive_tools.decision_for_pending([
        {"name": "todo", "args": {"todos": []}, "id": "call_todo"},
    ])
    assert decision.action == "skip"
    assert "Avoid repeating state updates" in decision.message

    second_decision = guards.repetitive_tools.decision_for_pending([
        {"name": "todo", "args": {"todos": []}, "id": "call_todo_again"},
    ])
    assert second_decision.action == "terminate"
    assert "stopped this turn" in second_decision.message


def test_repetitive_tool_cycle_ignores_exempt_tools():
    guards = RuntimeGuardState()
    summary = ToolCycleSummary(
        tool_names=["bash"],
        only_tool="bash",
        call_count=1,
        has_progress=False,
    )

    assert guards.repetitive_tools.record_cycle(summary) is None
    assert guards.repetitive_tools.record_cycle(summary) is None
    decision = guards.repetitive_tools.decision_for_pending([
        {"name": "bash", "args": {"command": "pwd"}, "id": "call_bash"},
    ])
    assert decision.action == "allow"


def test_repetitive_tool_cycle_resets_after_progress():
    guards = RuntimeGuardState()
    summary = ToolCycleSummary(
        tool_names=["todo"],
        only_tool="todo",
        call_count=1,
        has_progress=False,
    )
    progress = ToolCycleSummary(
        tool_names=["read"],
        only_tool="read",
        call_count=1,
        has_progress=True,
    )

    guards.repetitive_tools.record_cycle(summary)
    warning = guards.repetitive_tools.record_cycle(summary)
    assert warning is not None
    assert guards.repetitive_tools.warned_tool == "todo"

    guards.repetitive_tools.record_cycle(progress)
    assert guards.repetitive_tools.warned_tool == ""
    assert guards.repetitive_tools.skipped_tool == ""

    assert guards.repetitive_tools.record_cycle(summary) is None
    warning_again = guards.repetitive_tools.record_cycle(summary)
    assert warning_again is not None
    assert guards.repetitive_tools.warned_tool == "todo"


def test_no_progress_guard_warns_then_terminates_and_resets_on_progress():
    guards = RuntimeGuardState()
    stalled = ToolCycleSummary(
        tool_names=["checkpoint"],
        only_tool="checkpoint",
        call_count=1,
        has_progress=False,
    )

    assert guards.no_progress.record_cycle(stalled) is None
    assert guards.no_progress.record_cycle(stalled) is None

    warning = guards.no_progress.record_cycle(stalled)
    assert warning is not None
    assert warning.level == "light"
    assert "No meaningful progress" in warning.message
    assert guards.no_progress.decision().action == "allow"

    assert guards.no_progress.record_cycle(stalled) is None
    terminate = guards.no_progress.record_cycle(stalled)
    assert terminate is None

    decision = guards.no_progress.decision()
    assert decision.action == "terminate"
    assert "No meaningful progress" in decision.message

    progressed = ToolCycleSummary(
        tool_names=["read"],
        only_tool="read",
        call_count=1,
        has_progress=True,
    )
    assert guards.no_progress.record_cycle(progressed) is None
    assert guards.no_progress.consecutive == 0
    assert guards.no_progress.decision().action == "allow"


def test_no_progress_guard_counts_repeated_same_evidence_as_stalled():
    guards = RuntimeGuardState()
    stalled = ToolCycleSummary(
        tool_names=["checkpoint"],
        only_tool="checkpoint",
        call_count=1,
        has_progress=False,
    )

    assert guards.no_progress.record_cycle(stalled) is None
    assert guards.no_progress.record_cycle(stalled) is None
    assert guards.no_progress.consecutive == 2

    first_read = cycle_summary_from_tools([{
        "tool_call": {"name": "read", "args": {"file_path": "same.py"}},
        "result": ToolResult(output="same contents"),
    }])
    assert first_read.has_progress is False
    assert first_read.evidence_keys
    assert guards.no_progress.record_cycle(first_read) is None
    assert guards.no_progress.consecutive == 0

    repeated_read = cycle_summary_from_tools([{
        "tool_call": {"name": "read", "args": {"file_path": "same.py"}},
        "result": ToolResult(output="same contents"),
    }])
    assert guards.no_progress.record_cycle(repeated_read) is None
    assert guards.no_progress.consecutive == 1

    changed_read = cycle_summary_from_tools([{
        "tool_call": {"name": "read", "args": {"file_path": "same.py"}},
        "result": ToolResult(output="changed contents"),
    }])
    assert guards.no_progress.record_cycle(changed_read) is None
    assert guards.no_progress.consecutive == 0


def test_wall_clock_guard_has_subagent_preset():
    guard = WallClockGuardState.for_subagent()

    assert guard.limit_seconds == 1800.0


def test_wall_clock_guard_default_is_disabled():
    guard = WallClockGuardState()
    assert guard.limit_seconds == 0.0
    decision = guard.record_check(now=99999.0, label="voidx")
    assert decision.action == "allow"


def test_wall_clock_guard_terminates_at_limit():
    guard = WallClockGuardState(started_at=100.0, limit_seconds=1800.0)

    decision = guard.record_check(now=1899.0, label="sub", latest_action="grep src/")
    assert decision.action == "allow"

    decision = guard.record_check(now=1901.0, label="sub", latest_action="read tests/")
    assert decision.action == "terminate"
    assert "30m01s" in decision.message

    decision = guard.record_check(now=99999.0, label="sub")
    assert decision.action == "allow"


def test_error_kind_from_result_does_not_match_generic_exception_text():
    assert error_kind_from_result(ToolResult(output="no exception was raised")) == "unknown_error"
    assert error_kind_from_result(ToolResult(output="exception occurred in tool")) == "tool_exception"


def test_cycle_summary_uses_truncated_evidence_for_large_outputs():
    base = "x" * 500
    first = [{
        "tool_call": {"name": "bash", "args": {"command": "printf test"}},
        "result": ToolResult(output=base + "a"),
    }]
    second = [{
        "tool_call": {"name": "bash", "args": {"command": "printf test"}},
        "result": ToolResult(output=base + "b"),
    }]

    assert cycle_summary_from_tools(first).evidence_keys == cycle_summary_from_tools(second).evidence_keys
