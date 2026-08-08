import sys
from pathlib import Path


from voidx.agent.adapters.langgraph.runtime.runtime_guards import (
    RuntimeGuardState,
    ToolCycleSummary,
    WallClockGuardState,
    build_failure_key,
    cycle_summary_from_tools,
    error_kind_from_result,
    todo_status_signature,
)
from voidx.tooling.domain.result import ToolResult


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
    original = {"name": "search", "args": {"pattern": "old", "path": "src"}}
    changed = {"name": "search", "args": {"pattern": "new", "path": "src"}}
    result = ToolResult(output="grep failed", metadata={"error": True, "error_kind": "unknown_error"})
    key = build_failure_key(original, result)

    guards.tool_failures.record_failure(key, "grep failed")
    guards.tool_failures.record_failure(key, "grep failed")
    guards.tool_failures.record_failure(key, "grep failed")

    assert guards.tool_failures.should_block(original) is True
    assert guards.tool_failures.should_block(changed) is False


def test_failure_loop_does_not_block_different_manage_paths():
    guards = RuntimeGuardState()
    original = {"name": "manage", "args": {"op": "delete", "paths": "stale.py"}}
    changed = {"name": "manage", "args": {"op": "delete", "paths": "other.py"}}
    result = ToolResult(output="delete failed", metadata={"error": True, "error_kind": "unknown_error"})
    key = build_failure_key(original, result)

    guards.tool_failures.record_failure(key, "delete failed")
    guards.tool_failures.record_failure(key, "delete failed")
    guards.tool_failures.record_failure(key, "delete failed")

    assert guards.tool_failures.should_block(original) is True
    assert guards.tool_failures.should_block(changed) is False


def test_failure_loop_does_not_block_different_manage_moves():
    guards = RuntimeGuardState()
    original = {"name": "manage", "args": {"op": "move", "moves": [{"src": "old.py", "dest": "new.py"}]}}
    changed = {"name": "manage", "args": {"op": "move", "moves": [{"src": "other.py", "dest": "new.py"}]}}
    result = ToolResult(output="move failed", metadata={"error": True, "error_kind": "unknown_error"})
    key = build_failure_key(original, result)

    guards.tool_failures.record_failure(key, "move failed")
    guards.tool_failures.record_failure(key, "move failed")
    guards.tool_failures.record_failure(key, "move failed")

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
        "active": 0,
        "pending": 2,
    }
    rewritten = {
        "total": 2,
        "done": 0,
        "active": 0,
        "pending": 2,
    }
    progressed = {
        "total": 2,
        "done": 1,
        "active": 1,
        "pending": 0,
    }

    assert todo_status_signature(first) == todo_status_signature(rewritten)
    assert todo_status_signature(first) != todo_status_signature(progressed)


def test_repetitive_todo_cycle_warns_then_skips_then_terminates():
    guards = RuntimeGuardState()
    summary = ToolCycleSummary(
        tool_names=["todo"],
        only_tool="todo:read",
        call_count=1,
        has_progress=False,
    )

    assert guards.repetitive_tools.record_cycle(summary) is None
    warning = guards.repetitive_tools.record_cycle(summary)
    assert warning is not None
    assert warning.level == "light"
    assert "only called todo:read" in warning.message

    decision = guards.repetitive_tools.decision_for_pending([
        {"name": "todo", "args": {"op": "read"}, "id": "call_todo"},
    ])
    assert decision.action == "skip"
    assert "Avoid repeating state updates" in decision.message

    second_decision = guards.repetitive_tools.decision_for_pending([
        {"name": "todo", "args": {"op": "read"}, "id": "call_todo_again"},
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
        only_tool="todo:read",
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
    assert guards.repetitive_tools.warned_tool == "todo:read"

    guards.repetitive_tools.record_cycle(progress)
    assert guards.repetitive_tools.warned_tool == ""
    assert guards.repetitive_tools.skipped_tool == ""

    assert guards.repetitive_tools.record_cycle(summary) is None
    warning_again = guards.repetitive_tools.record_cycle(summary)
    assert warning_again is not None
    assert guards.repetitive_tools.warned_tool == "todo:read"


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




def test_no_progress_guard_terminates_repeated_workflow_guidance():
    guards = RuntimeGuardState()
    summary = cycle_summary_from_tools([{
        "tool_call": {"name": "workflow", "args": {"action": "advance", "workflow": "debug"}},
        "result": ToolResult(
            output="Workflow node 'debug' is not currently active.",
            metadata={"workflow_guidance": {"applied": False, "reason": "invalid_active_workflow"}},
        ),
    }])

    assert summary.has_progress is False
    assert summary.evidence_keys == []
    for _ in range(5):
        guards.no_progress.record_cycle(summary)

    assert guards.no_progress.decision().action == "terminate"
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


def test_replace_same_file_different_lines_not_blocked():
    """同文件不同行范围的 replace 失败不应互相拉黑。

    归并键应包含 start_no/end_no，而非仅 file_path。
    """
    guards = RuntimeGuardState()
    result = ToolResult(output="anchor mismatch", metadata={"error": True, "error_kind": "unknown_error"})

    failing = {"name": "replace", "args": {"file_path": "a.py", "start_no": 1, "end_no": 1, "start_anchor": "x", "end_anchor": "x", "new_string": "y"}}
    key = build_failure_key(failing, result)
    guards.tool_failures.record_failure(key, "anchor mismatch")
    guards.tool_failures.record_failure(key, "anchor mismatch")
    guards.tool_failures.record_failure(key, "anchor mismatch")

    assert guards.tool_failures.should_block(failing) is True

    different_lines = {"name": "replace", "args": {"file_path": "a.py", "start_no": 10, "end_no": 10, "start_anchor": "x", "end_anchor": "x", "new_string": "y"}}
    assert guards.tool_failures.should_block(different_lines) is False


def test_replace_same_lines_repeated_failure_blocked():
    """同文件同行范围的 replace 反复失败仍应被拉黑（保留防死循环）。"""
    guards = RuntimeGuardState()
    result = ToolResult(output="anchor mismatch", metadata={"error": True, "error_kind": "unknown_error"})

    call = {"name": "replace", "args": {"file_path": "a.py", "start_no": 1, "end_no": 1, "start_anchor": "x", "end_anchor": "x", "new_string": "y"}}
    key = build_failure_key(call, result)
    guards.tool_failures.record_failure(key, "anchor mismatch")
    guards.tool_failures.record_failure(key, "anchor mismatch")
    guards.tool_failures.record_failure(key, "anchor mismatch")

    assert guards.tool_failures.should_block(call) is True


def test_replace_same_lines_different_anchor_same_key():
    """同行范围不同 anchor 的 replace 失败应归并为同一 key。

    anchor 是内容校验项，非定位项；同位置不同 anchor 的失败应合并计数。
    """
    result = ToolResult(output="anchor mismatch", metadata={"error": True, "error_kind": "unknown_error"})

    call_x = {"name": "replace", "args": {"file_path": "a.py", "start_no": 1, "end_no": 1, "start_anchor": "x", "end_anchor": "x", "new_string": "y"}}
    call_y = {"name": "replace", "args": {"file_path": "a.py", "start_no": 1, "end_no": 1, "start_anchor": "z", "end_anchor": "z", "new_string": "w"}}

    key_x = build_failure_key(call_x, result)
    key_y = build_failure_key(call_y, result)

    assert key_x.stable_key == key_y.stable_key


def test_replace_success_clears_blocks():
    """成功调用应解除同 tool_name 的黑名单（按 tool_name 前缀清理）。"""
    guards = RuntimeGuardState()
    result = ToolResult(output="anchor mismatch", metadata={"error": True, "error_kind": "unknown_error"})

    failing = {"name": "replace", "args": {"file_path": "a.py", "start_no": 1, "end_no": 1, "start_anchor": "x", "end_anchor": "x", "new_string": "y"}}
    key = build_failure_key(failing, result)
    guards.tool_failures.record_failure(key, "anchor mismatch")
    guards.tool_failures.record_failure(key, "anchor mismatch")
    guards.tool_failures.record_failure(key, "anchor mismatch")
    assert guards.tool_failures.should_block(failing) is True

    recovered = {"name": "replace", "args": {"file_path": "a.py", "start_no": 5, "end_no": 5, "start_anchor": "x", "end_anchor": "x", "new_string": "y"}}
    guards.tool_failures.record_success(recovered)
    assert guards.tool_failures.should_block(failing) is False


def test_failure_loop_aggregates_policy_blocked_calls_across_rephrased_commands():
    """Policy-blocked calls are static denials: rephrasing args must not reset the loop counter."""
    from voidx.agent.adapters.langgraph.runtime.runtime_guards import ToolFailureLoopState

    guards = ToolFailureLoopState()
    blocked = ToolResult(
        output='{"ok": false, "stderr": "shell policy deferred: nested interpreter", "blocked": true}',
        metadata={"blocked": True, "error": True},
    )
    first = build_failure_key({"name": "bash", "args": {"command": "python x.py"}}, blocked)
    second = build_failure_key({"name": "bash", "args": {"command": "env PYTHONPATH=src python3 x.py"}}, blocked)

    assert first.stable_key == second.stable_key
    assert guards.record_failure(first, "blocked") is None
    guidance = guards.record_failure(second, "blocked")
    assert guidance is not None
    assert "failed twice" in guidance.message


def test_no_progress_subagent_guidance_tells_child_to_return_findings():
    from voidx.agent.adapters.langgraph.runtime.runtime_guards import NoProgressState

    state = NoProgressState(for_subagent=True)
    summary = ToolCycleSummary(tool_names=["bash"], only_tool="bash", call_count=1)

    guidance = None
    for _ in range(3):
        guidance = state.record_cycle(summary) or guidance

    assert guidance is not None
    assert "cannot ask the user" in guidance.message
    assert "final answer" in guidance.message
