from __future__ import annotations

import pytest

from voidx.agent.domain.loop import (
    LOOP_PROFILE,
    LoopDecision,
    LoopMode,
    LoopSpec,
    LoopToolView,
)


def test_loop_decision_accepts_all_lifecycle_outcomes() -> None:
    outcomes = ["continue", "completed", "blocked", "needs_user", "failed", "stop"]

    decisions = [LoopDecision(outcome=outcome, summary=f"{outcome} summary") for outcome in outcomes]

    assert [decision.outcome for decision in decisions] == outcomes


def test_loop_spec_distinguishes_fixed_and_dynamic_modes() -> None:
    dynamic = LoopSpec(prompt="check deploy")
    fixed = LoopSpec(prompt="check deploy", interval_seconds=300)

    assert dynamic.mode is LoopMode.DYNAMIC
    assert dynamic.loop_thread_id("parent-1") == "loop:parent-1:active"
    assert fixed.mode is LoopMode.FIXED
    assert fixed.interval_seconds == 300


def test_loop_spec_rejects_empty_prompt() -> None:
    with pytest.raises(ValueError):
        LoopSpec(prompt="   ")


def test_loop_tool_view_is_closed_world_for_automatic_wakeups() -> None:
    available = {
        "read",
        "search",
        "websearch",
        "loop",
        "schedule_wakeup",
        "clarify",
        "checkpoint",
        "agent",
        "bash",
        "write",
        "workflow",
        "todo",
    }

    view = LoopToolView.default(workflow_enabled=False).bind(available)

    assert "loop" in view.bound_tool_ids
    assert "read" in view.bound_tool_ids
    assert "websearch" in view.bound_tool_ids
    assert "schedule_wakeup" not in view.bound_tool_ids
    assert "clarify" not in view.bound_tool_ids
    assert "checkpoint" not in view.bound_tool_ids
    assert "agent" not in view.bound_tool_ids
    assert "bash" in view.bound_tool_ids
    assert "write" not in view.bound_tool_ids
    assert "workflow" not in view.bound_tool_ids
    assert "todo" not in view.bound_tool_ids


def test_loop_tool_view_can_expose_workflow_subset_when_enabled() -> None:
    view = LoopToolView.default(workflow_enabled=True).bind(
        {"loop", "workflow", "task_status", "todo", "clarify"}
    )

    assert {"loop", "workflow", "task_status", "todo"}.issubset(view.bound_tool_ids)
    assert "clarify" not in view.bound_tool_ids


def test_loop_profile_is_first_class_profile() -> None:
    assert LOOP_PROFILE.profile_id == "loop"
    assert LOOP_PROFILE.name == "Loop"


def test_loop_spec_generation_drives_thread_and_session_id() -> None:
    default = LoopSpec(prompt="check")
    gen2 = LoopSpec(prompt="check", generation="20260728-01")

    assert default.loop_thread_id("parent-1") == "loop:parent-1:active"
    assert gen2.loop_thread_id("parent-1") == "loop:parent-1:20260728-01"
    assert gen2.loop_session_id("parent-1") == "loop:parent-1:20260728-01"


def test_loop_spec_rejects_empty_generation() -> None:
    with pytest.raises(ValueError):
        LoopSpec(prompt="check", generation="  ")


def test_loop_tool_view_bash_requests_approval() -> None:
    view = LoopToolView.default(workflow_enabled=False).bind({"bash", "read", "loop"})

    bash_decision = view.check_tool_call("bash", {"command": "pytest -q"})
    assert bash_decision.allowed is True
    assert bash_decision.requests_approval is True

    read_decision = view.check_tool_call("read", {"file_path": "/tmp/x"})
    assert read_decision.allowed is True
    assert read_decision.requests_approval is False

    loop_decision = view.check_tool_call("loop", {"op": "stop"})
    assert loop_decision.allowed is True
    assert loop_decision.requests_approval is False