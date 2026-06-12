import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.runtime_context import TaskIntent
from voidx.agent.task_state import GoalType, PendingApproval, TaskState, resolve_turn_intent


def test_inspect_turn_is_coding_without_implementation_approval():
    state = TaskState()

    resolution = resolve_turn_intent("看看 voidx 的 agent 编排", "auto", state)
    state.update_after_turn(resolution, "看看 voidx 的 agent 编排")

    assert resolution.intent == TaskIntent.CODING
    assert resolution.goal is None
    assert state.current_goal is None
    assert state.pending_approval is None


def test_design_turn_opens_one_pending_implementation_approval():
    state = TaskState()

    resolution = resolve_turn_intent("给个优化方案", "auto", state)
    state.update_after_turn(resolution, "给个优化方案")

    assert resolution.intent == TaskIntent.CODING
    assert resolution.goal is None
    assert state.current_goal is None
    assert state.pending_approval is None


def test_approval_phrase_confirms_pending_design():
    state = TaskState()
    state.pending_approval = PendingApproval(scope="给个优化方案", source_goal_type=GoalType.DESIGN)

    resolution = resolve_turn_intent("对，可以", "auto", state)
    state.update_after_turn(resolution, "对，可以")

    assert resolution.intent == TaskIntent.CODING
    assert resolution.goal is None
    assert resolution.confirmed_approval is not None
    assert resolution.confirmed_approval.scope == "给个优化方案"
    assert state.current_goal is not None
    assert state.current_goal.type == GoalType.FEATURE
    assert state.current_goal.user_requested_write is True
    assert state.pending_approval is None


def test_confirm_phrase_confirms_pending_design():
    state = TaskState()
    state.pending_approval = PendingApproval(scope="给个优化方案", source_goal_type=GoalType.DESIGN)

    resolution = resolve_turn_intent("确认", "auto", state)

    assert resolution.intent == TaskIntent.CODING
    assert resolution.goal is None
    assert resolution.confirmed_approval is not None
    assert resolution.confirmed_approval.scope == "给个优化方案"


def test_approval_phrase_without_pending_design_is_general_confirmation_needed():
    resolution = resolve_turn_intent("对，可以", "auto", TaskState())

    assert resolution.intent == TaskIntent.GENERAL
    assert resolution.goal is None


def test_direct_implementation_request_does_not_need_pending_design():
    resolution = resolve_turn_intent("开始实现这个优化", "auto", TaskState())

    assert resolution.intent == TaskIntent.CODING
    assert resolution.goal is None
    assert resolution.confirmed_approval is None


def test_short_modify_command_is_explicit_write_request():
    resolution = resolve_turn_intent("改", "auto", TaskState())

    assert resolution.intent == TaskIntent.CODING
    assert resolution.goal is None
    assert resolution.reason == "direct short command asks to modify the current task"


def test_design_question_with_change_word_stays_design_goal():
    resolution = resolve_turn_intent("怎么改比较好", "auto", TaskState())

    assert resolution.intent == TaskIntent.CODING
    assert resolution.goal is None


def test_intent_classifier_uses_recent_two_turn_window_for_short_input():
    state = TaskState()
    first = resolve_turn_intent("看看这个bug", "auto", state)
    state.update_after_turn(first, "看看这个bug")

    resolution = resolve_turn_intent("这个", "auto", state)

    assert state.intent_window_text("这个") == "看看这个bug [SEP] 这个"
    assert resolution.intent == TaskIntent.CODING
    assert resolution.goal is None


def test_intent_window_keeps_only_two_recent_user_inputs():
    state = TaskState()

    for text in ["第一轮", "第二轮", "第三轮"]:
        resolution = resolve_turn_intent(text, "auto", state)
        state.update_after_turn(resolution, text)

    assert state.recent_user_texts == ["第二轮", "第三轮"]
    assert state.intent_window_text("第四轮") == "第三轮 [SEP] 第四轮"


def test_intent_window_does_not_override_approval_without_pending_plan():
    state = TaskState(recent_user_texts=["给个重构方案"])

    resolution = resolve_turn_intent("可以", "auto", state)

    assert resolution.intent == TaskIntent.GENERAL
    assert resolution.reason == "approval phrase without a pending implementation plan"


def test_intent_window_does_not_override_direct_short_command():
    state = TaskState(recent_user_texts=["看看这个模块"])

    resolution = resolve_turn_intent("改", "auto", state)

    assert resolution.intent == TaskIntent.CODING
    assert resolution.reason == "direct short command asks to modify the current task"


def test_general_turn_clears_pending_approval():
    state = TaskState()
    state.pending_approval = PendingApproval(scope="给个优化方案", source_goal_type=GoalType.DESIGN)

    general = resolve_turn_intent("谢谢", "auto", state)
    state.update_after_turn(general, "谢谢")
    approval = resolve_turn_intent("对，可以", "auto", state)

    assert general.intent == TaskIntent.GENERAL
    assert general.goal is None
    assert state.current_goal is None
    assert state.pending_approval is None
    assert approval.intent == TaskIntent.GENERAL


def test_goal_mode_uses_task_state_current_goal():
    state = TaskState()
    state.set_goal("优化 markdown 渲染截断")

    resolution = resolve_turn_intent("给个方案", "goal", state)
    state.update_after_turn(resolution, "给个方案", scope_text=state.current_goal.label)

    assert state.current_goal is not None
    assert state.current_goal.type == GoalType.CHORE
    assert state.current_goal.target == "优化 markdown 渲染截断"
    assert state.pending_approval is None


def test_goal_mode_confirmation_clears_pending_approval():
    state = TaskState()
    state.set_goal("优化 markdown 渲染截断")
    state.pending_approval = PendingApproval(
        scope=state.current_goal.label,
        source_goal_type=GoalType.DESIGN,
    )

    approval = resolve_turn_intent("对，可以", "goal", state)
    state.update_after_turn(approval, "对，可以", scope_text=state.current_goal.label)

    assert approval.intent == TaskIntent.CODING
    assert state.current_goal is not None
    assert state.current_goal.user_requested_write is True
    assert state.pending_approval is None


def test_clear_goal_resets_goal_state():
    state = TaskState()
    state.set_goal("修复 UI")

    state.clear_goal()

    assert state.current_goal is None
    assert state.pending_approval is None
    assert state.workflow_runs == {}
