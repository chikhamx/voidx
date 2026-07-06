import sys
from pathlib import Path


from voidx.agent.graph.runtime_guards import (
    RuntimeGuardState,
    ToolCycleSummary,
    cycle_summary_from_tools,
    tool_op_key,
    only_tool_key,
    LOW_VALUE_REPETITIVE_TOOL_KEYS,
)
from voidx.tools.base import ToolResult


# ── tool_op_key ───────────────────────────────────────────────

def test_tool_op_key_todo_write():
    assert tool_op_key({"name": "todo", "args": {"op": "write"}}) == "todo:write"


def test_tool_op_key_todo_read():
    assert tool_op_key({"name": "todo", "args": {"op": "read"}}) == "todo:read"


def test_tool_op_key_todo_update():
    assert tool_op_key({"name": "todo", "args": {"op": "update"}}) == "todo:update"


def test_tool_op_key_todo_missing_op_defaults_to_read():
    assert tool_op_key({"name": "todo", "args": {}}) == "todo:read"


def test_tool_op_key_workflow_advance():
    assert tool_op_key({"name": "workflow", "args": {"action": "advance"}}) == "workflow:advance"


def test_tool_op_key_workflow_enter():
    assert tool_op_key({"name": "workflow", "args": {"action": "enter"}}) == "workflow:enter"


def test_tool_op_key_workflow_done():
    assert tool_op_key({"name": "workflow", "args": {"action": "done"}}) == "workflow:done"


def test_tool_op_key_workflow_missing_action_defaults_to_name():
    assert tool_op_key({"name": "workflow", "args": {}}) == "workflow"


def test_tool_op_key_bash_unchanged():
    assert tool_op_key({"name": "bash", "args": {"command": "pytest"}}) == "bash"


def test_tool_op_key_read_unchanged():
    assert tool_op_key({"name": "read", "args": {"file_path": "x.py"}}) == "read"


# ── only_tool_key ─────────────────────────────────────────────

def test_only_tool_key_single_tool():
    calls = [{"name": "todo", "args": {"op": "write"}}]
    assert only_tool_key(calls) == "todo:write"


def test_only_tool_key_mixed_ops_returns_empty():
    calls = [
        {"name": "todo", "args": {"op": "write"}},
        {"name": "todo", "args": {"op": "read"}},
    ]
    assert only_tool_key(calls) == ""


def test_only_tool_key_all_same_op():
    calls = [
        {"name": "workflow", "args": {"action": "advance"}},
        {"name": "workflow", "args": {"action": "advance"}},
    ]
    assert only_tool_key(calls) == "workflow:advance"


# ── LOW_VALUE_REPETITIVE_TOOL_KEYS ────────────────────────────

def test_low_value_keys_contains_todo_read():
    assert "todo:read" in LOW_VALUE_REPETITIVE_TOOL_KEYS


def test_low_value_keys_contains_checkpoint():
    assert "checkpoint" in LOW_VALUE_REPETITIVE_TOOL_KEYS


def test_low_value_keys_excludes_todo_write():
    assert "todo:write" not in LOW_VALUE_REPETITIVE_TOOL_KEYS


def test_low_value_keys_contains_workflow_advance():
    assert "workflow:advance" in LOW_VALUE_REPETITIVE_TOOL_KEYS


# ── is_stuck with tool_op_key awareness ───────────────────────

def test_stuck_does_not_trigger_for_todo_write():
    guards = RuntimeGuardState()
    summary = ToolCycleSummary(
        tool_names=["todo"],
        only_tool="todo:write",
        call_count=1,
        has_progress=True,
    )
    guards.repetitive_tools.record_cycle(summary)
    result = guards.repetitive_tools.record_cycle(summary)
    assert result is None  # no warning for todo:write


def test_stuck_does_trigger_for_todo_read():
    guards = RuntimeGuardState()
    summary = ToolCycleSummary(
        tool_names=["todo"],
        only_tool="todo:read",
        call_count=1,
        has_progress=False,
    )
    guards.repetitive_tools.record_cycle(summary)
    warning = guards.repetitive_tools.record_cycle(summary)
    assert warning is not None
    assert warning.level == "light"


# ── decision_for_pending with tool_op_key awareness ───────────


def test_stuck_does_trigger_for_workflow_guidance_advance():
    guards = RuntimeGuardState()
    summary = ToolCycleSummary(
        tool_names=["workflow"],
        only_tool="workflow:advance",
        call_count=1,
        has_progress=False,
    )
    guards.repetitive_tools.record_cycle(summary)
    warning = guards.repetitive_tools.record_cycle(summary)
    assert warning is not None
    assert warning.level == "light"
    assert "workflow:advance" in warning.message

def test_decision_allows_todo_write():
    guards = RuntimeGuardState()
    # warm up: two cycles of todo:write to trigger warning
    summary = ToolCycleSummary(
        tool_names=["todo"],
        only_tool="todo:write",
        call_count=1,
        has_progress=True,
    )
    guards.repetitive_tools.record_cycle(summary)
    guards.repetitive_tools.record_cycle(summary)
    # todo:write is not in LOW_VALUE_REPETITIVE_TOOL_KEYS
    decision = guards.repetitive_tools.decision_for_pending([
        {"name": "todo", "args": {"op": "write"}, "id": "call_1"},
    ])
    assert decision.action == "allow"


def test_decision_skips_todo_read():
    guards = RuntimeGuardState()
    summary = ToolCycleSummary(
        tool_names=["todo"],
        only_tool="todo:read",
        call_count=1,
        has_progress=False,
    )
    guards.repetitive_tools.record_cycle(summary)
    guards.repetitive_tools.record_cycle(summary)  # triggers warning
    decision = guards.repetitive_tools.decision_for_pending([
        {"name": "todo", "args": {"op": "read"}, "id": "call_1"},
    ])
    assert decision.action == "skip"


# ── cycle_summary_from_tools: progress ────────────────────────
# 使用 dataclass 构造 ExecutedTool-like 对象

def _executed(tool_name, args, *, ok=True, diff=None, metadata=None):
    """Minimal ExecutedTool-like dataclass for cycle_summary_from_tools."""
    from dataclasses import dataclass

    @dataclass
    class ExecutedTool:
        tool_call: dict
        result: object
        todo_state: object = None

    result = ToolResult(
        output="ok",
        diff=diff,
        metadata=metadata or {},
    )
    return ExecutedTool(
        tool_call={"name": tool_name, "args": args},
        result=result,
    )


def _result_ok(result):
    meta = getattr(result, "metadata", {}) or {}
    return not (meta.get("error") or meta.get("blocked"))


def test_cycle_summary_todo_write_has_progress():
    summary = cycle_summary_from_tools(
        [_executed("todo", {"op": "write"})],
        result_ok=_result_ok,
    )
    assert summary.has_progress is True


def test_cycle_summary_todo_update_has_progress():
    summary = cycle_summary_from_tools(
        [_executed("todo", {"op": "update"})],
        result_ok=_result_ok,
    )
    assert summary.has_progress is True


def test_cycle_summary_todo_read_no_progress():
    summary = cycle_summary_from_tools(
        [_executed("todo", {"op": "read"})],
        result_ok=_result_ok,
    )
    assert summary.has_progress is False


def test_cycle_summary_workflow_advance_has_progress():
    summary = cycle_summary_from_tools(
        [_executed("workflow", {"action": "advance"})],
        result_ok=_result_ok,
    )
    assert summary.has_progress is True


def test_cycle_summary_workflow_guidance_no_progress_or_evidence():
    summary = cycle_summary_from_tools(
        [_executed(
            "workflow",
            {"action": "advance", "workflow": "debug"},
            metadata={"workflow_guidance": {"applied": False, "reason": "invalid_active_workflow"}},
        )],
        result_ok=_result_ok,
    )
    assert summary.has_progress is False
    assert summary.evidence_keys == []


def test_cycle_summary_workflow_success_progress():
    summary = cycle_summary_from_tools(
        [_executed(
            "workflow",
            {"action": "advance"},
            metadata={"workflow_transition": {"from": "debug", "to": "tdd"}},
        )],
        result_ok=_result_ok,
    )
    assert summary.has_progress is True


def test_cycle_summary_workflow_done_has_progress():
    summary = cycle_summary_from_tools(
        [_executed("workflow", {"action": "done"})],
        result_ok=_result_ok,
    )
    assert summary.has_progress is True


def test_cycle_summary_todo_write_contributes_evidence():
    summary = cycle_summary_from_tools(
        [_executed("todo", {"op": "write", "todos": [{"id": "a", "content": "do x", "status": "pending"}]})],
        result_ok=_result_ok,
    )
    assert len(summary.evidence_keys) > 0


def test_cycle_summary_todo_read_no_evidence():
    summary = cycle_summary_from_tools(
        [_executed("todo", {"op": "read", "filter": "all"})],
        result_ok=_result_ok,
    )
    assert len(summary.evidence_keys) == 0


def test_cycle_summary_only_tool_is_tool_op_key():
    calls = [_executed("todo", {"op": "write"})]
    summary = cycle_summary_from_tools(calls, result_ok=_result_ok)
    assert summary.only_tool == "todo:write"


def test_cycle_summary_only_tool_empty_for_mixed_ops():
    calls = [
        _executed("todo", {"op": "write"}),
        _executed("todo", {"op": "read"}),
    ]
    summary = cycle_summary_from_tools(calls, result_ok=_result_ok)
    assert summary.only_tool == ""
