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
        "loop_update",
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

    assert "loop_update" in view.bound_tool_ids
    assert "read" in view.bound_tool_ids
    assert "websearch" in view.bound_tool_ids
    assert "schedule_wakeup" not in view.bound_tool_ids
    assert "clarify" not in view.bound_tool_ids
    assert "checkpoint" not in view.bound_tool_ids
    assert "agent" not in view.bound_tool_ids
    assert "bash" not in view.bound_tool_ids
    assert "write" not in view.bound_tool_ids
    assert "workflow" not in view.bound_tool_ids
    assert "todo" not in view.bound_tool_ids


def test_loop_tool_view_can_expose_workflow_subset_when_enabled() -> None:
    view = LoopToolView.default(workflow_enabled=True).bind(
        {"loop_update", "workflow", "task_status", "todo", "clarify"}
    )

    assert {"loop_update", "workflow", "task_status", "todo"}.issubset(view.bound_tool_ids)
    assert "clarify" not in view.bound_tool_ids


def test_loop_profile_is_first_class_profile() -> None:
    assert LOOP_PROFILE.profile_id == "loop"
    assert LOOP_PROFILE.name == "Loop"
