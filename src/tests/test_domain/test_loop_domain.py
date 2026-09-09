from __future__ import annotations

import pytest

from voidx.agent.domain.automation.loop import (
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


def test_loop_spec_rejects_non_integer_interval_seconds() -> None:
    with pytest.raises(Exception):
        LoopSpec(prompt="check deploy", interval_seconds=1.5)


def test_loop_tool_view_phase_bindings() -> None:
    available = {
        "read", "find", "search", "loop", "loop_init", "loop_start", "loop_commit", "clarify",
    }
    idle_view = LoopToolView.default(phase="idle").bind(available)
    assert "loop_init" in idle_view.bound_tool_ids
    assert "loop_start" not in idle_view.bound_tool_ids
    assert "loop_commit" not in idle_view.bound_tool_ids

    work_view = LoopToolView.default(phase="work").bind(available)
    assert "loop_init" not in work_view.bound_tool_ids
    assert "loop_start" in work_view.bound_tool_ids
    assert "loop_commit" in work_view.bound_tool_ids
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
        {"loop", "workflow", "todo", "clarify"}
    )

    assert {"loop", "workflow", "todo"}.issubset(view.bound_tool_ids)
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


# ── Loop guardrails: stall / missing-decision auto-pause ─────────────────────

from voidx.agent.domain.automation.loop import (
    LOOP_MISSING_DECISION_LIMIT,
    LOOP_STALL_LIMIT,
    NO_LOOP_DECISION_REASON,
    apply_loop_guardrails,
)
from voidx.agent.domain.thread import (
    DecisionMetadata,
    LoopGuardrailState,
    RuntimeDecision,
)


def _continue_decision(progress: str = "none", reason: str = "") -> RuntimeDecision:
    return RuntimeDecision(
        outcome="continue",
        summary="iteration summary",
        progress=progress,
        next_delay_seconds=60,
        reason=reason,
    )


def _previous_with_guardrail(stall: int = 0, missing: int = 0) -> RuntimeDecision:
    return RuntimeDecision(
        outcome="continue",
        summary="previous iteration",
        progress="none",
        metadata=DecisionMetadata(
            loop_guardrail=LoopGuardrailState(
                stall_count=stall,
                missing_decision_count=missing,
            )
        ),
    )


def test_loop_guardrail_counts_none_progress_from_scratch() -> None:
    decision = apply_loop_guardrails(None, _continue_decision(progress="none"))

    assert decision.outcome == "continue"
    assert decision.metadata is not None
    assert decision.metadata.loop_guardrail is not None
    assert decision.metadata.loop_guardrail.stall_count == 1
    assert decision.metadata.loop_guardrail.missing_decision_count == 0


def test_loop_guardrail_chains_counts_from_previous_decision() -> None:
    previous = _previous_with_guardrail(stall=2, missing=0)

    decision = apply_loop_guardrails(previous, _continue_decision(progress="none"))

    assert decision.metadata.loop_guardrail.stall_count == 3


def test_loop_guardrail_resets_stall_on_partial_progress() -> None:
    previous = _previous_with_guardrail(stall=3)

    decision = apply_loop_guardrails(previous, _continue_decision(progress="partial"))

    assert decision.outcome == "continue"
    assert decision.metadata.loop_guardrail.stall_count == 0


def test_loop_guardrail_pauses_at_stall_limit() -> None:
    previous = _previous_with_guardrail(stall=LOOP_STALL_LIMIT - 1)

    decision = apply_loop_guardrails(previous, _continue_decision(progress="none"))

    assert decision.outcome == "needs_user"
    assert decision.reason == "loop_stalled"
    assert decision.next_delay_seconds is None
    assert "no progress" in decision.summary


def test_loop_guardrail_pauses_at_missing_decision_limit() -> None:
    previous = _previous_with_guardrail(missing=LOOP_MISSING_DECISION_LIMIT - 1)

    decision = apply_loop_guardrails(
        previous, _continue_decision(reason=NO_LOOP_DECISION_REASON)
    )

    assert decision.outcome == "needs_user"
    assert decision.reason == "loop_decision_missing"
    assert decision.next_delay_seconds is None
    assert decision.metadata.loop_guardrail.missing_decision_count == LOOP_MISSING_DECISION_LIMIT


def test_loop_guardrail_missing_decision_also_counts_as_stall() -> None:
    previous = _previous_with_guardrail(stall=1, missing=1)

    decision = apply_loop_guardrails(
        previous, _continue_decision(reason=NO_LOOP_DECISION_REASON)
    )

    assert decision.outcome == "continue"
    assert decision.metadata.loop_guardrail.missing_decision_count == 2
    assert decision.metadata.loop_guardrail.stall_count == 2


def test_loop_guardrail_model_needs_user_resets_counts() -> None:
    previous = _previous_with_guardrail(stall=2, missing=1)

    decision = apply_loop_guardrails(
        previous,
        RuntimeDecision(outcome="needs_user", summary="pausing for user", progress="none"),
    )

    assert decision.outcome == "needs_user"
    assert decision.reason == ""
    assert decision.metadata.loop_guardrail.stall_count == 0
    assert decision.metadata.loop_guardrail.missing_decision_count == 0


def test_loop_guardrail_preserves_other_metadata_fields() -> None:
    decision = _continue_decision(progress="partial").model_copy(
        update={"metadata": DecisionMetadata(evidence_summary={"files": 2})}
    )

    guarded = apply_loop_guardrails(None, decision)

    assert guarded.metadata.evidence_summary == {"files": 2}
    assert guarded.metadata.loop_guardrail.stall_count == 0
