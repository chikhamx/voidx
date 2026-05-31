import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.runtime_context import TaskIntent
from voidx.agent.task_state import TaskPhase, TaskRun, TaskRunStatus, TaskState, resolve_turn_intent


def test_inspect_turn_does_not_allow_implementation():
    state = TaskState()

    resolution = resolve_turn_intent("看看 voidx 的 agent 编排", "auto", state)
    state.update_after_turn(resolution, "看看 voidx 的 agent 编排")

    assert resolution.intent == TaskIntent.INSPECT
    assert resolution.implementation_allowed is False
    assert state.awaiting_implementation_approval is False


def test_design_turn_opens_one_pending_implementation_approval():
    state = TaskState()

    resolution = resolve_turn_intent("给个优化方案", "auto", state)
    state.update_after_turn(resolution, "给个优化方案")

    assert resolution.intent == TaskIntent.DESIGN
    assert resolution.implementation_allowed is False
    assert state.awaiting_implementation_approval is True
    assert state.approved_scope == "给个优化方案"


def test_approval_phrase_confirms_pending_design():
    state = TaskState()
    design = resolve_turn_intent("给个优化方案", "auto", state)
    state.update_after_turn(design, "给个优化方案")

    resolution = resolve_turn_intent("对，可以", "auto", state)

    assert resolution.intent == TaskIntent.IMPLEMENT
    assert resolution.implementation_allowed is True
    assert resolution.approved_scope == "给个优化方案"


def test_confirm_phrase_confirms_pending_design():
    state = TaskState()
    design = resolve_turn_intent("给个优化方案", "auto", state)
    state.update_after_turn(design, "给个优化方案")

    resolution = resolve_turn_intent("确认", "auto", state)

    assert resolution.intent == TaskIntent.IMPLEMENT
    assert resolution.implementation_allowed is True
    assert resolution.approved_scope == "给个优化方案"


def test_approval_phrase_without_pending_design_is_ambiguous():
    resolution = resolve_turn_intent("对，可以", "auto", TaskState())

    assert resolution.intent == TaskIntent.AMBIGUOUS
    assert resolution.implementation_allowed is False


def test_direct_implementation_request_does_not_need_pending_design():
    resolution = resolve_turn_intent("开始实现这个优化", "auto", TaskState())

    assert resolution.intent == TaskIntent.IMPLEMENT
    assert resolution.implementation_allowed is True


def test_short_modify_command_is_explicit_implementation_intent():
    resolution = resolve_turn_intent("改", "auto", TaskState())

    assert resolution.intent == TaskIntent.IMPLEMENT
    assert resolution.implementation_allowed is True


def test_design_question_with_change_word_stays_design_intent():
    resolution = resolve_turn_intent("怎么改比较好", "auto", TaskState())

    assert resolution.intent == TaskIntent.DESIGN
    assert resolution.implementation_allowed is False


def test_non_design_turn_clears_pending_approval():
    state = TaskState()
    design = resolve_turn_intent("给个优化方案", "auto", state)
    state.update_after_turn(design, "给个优化方案")

    inspect = resolve_turn_intent("再看看 agent 编排", "auto", state)
    state.update_after_turn(inspect, "再看看 agent 编排")
    approval = resolve_turn_intent("对，可以", "auto", state)

    assert inspect.intent == TaskIntent.INSPECT
    assert state.awaiting_implementation_approval is False
    assert approval.intent == TaskIntent.AMBIGUOUS


def test_goal_run_tracks_goal_phase_and_approval_scope():
    run = TaskRun()
    run.set_goal("优化 markdown 渲染截断")

    design = resolve_turn_intent("给个方案", "goal", TaskState())
    run.update_after_turn(design, "给个方案", scope_text=run.goal)

    assert run.status == TaskRunStatus.ACTIVE
    assert run.phase == TaskPhase.DESIGN
    assert run.turn_count == 1
    assert run.awaiting_implementation_approval is True
    assert run.approved_scope == "优化 markdown 渲染截断"


def test_goal_run_implementation_clears_pending_approval():
    run = TaskRun()
    state = TaskState()
    run.set_goal("优化 markdown 渲染截断")
    design = resolve_turn_intent("给个方案", "goal", state)
    state.update_after_turn(design, "给个方案", scope_text=run.goal)
    run.update_after_turn(design, "给个方案", scope_text=run.goal)

    approval = resolve_turn_intent("对，可以", "goal", state)
    run.update_after_turn(approval, "对，可以", scope_text=run.goal)

    assert approval.intent == TaskIntent.IMPLEMENT
    assert run.phase == TaskPhase.IMPLEMENT
    assert run.turn_count == 2
    assert run.awaiting_implementation_approval is False
    assert run.approved_scope == ""


def test_goal_run_clear_resets_to_idle():
    run = TaskRun()
    run.set_goal("修复 UI")

    run.clear()

    assert run.goal == ""
    assert run.phase == TaskPhase.CLARIFY
    assert run.status == TaskRunStatus.IDLE
    assert run.turn_count == 0
