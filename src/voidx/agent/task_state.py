"""Compatibility exports for task intent state."""

from voidx.runtime.task_state import (
    Goal,
    GoalResolution,
    GoalType,
    IntentResolution,
    PendingApproval,
    TaskState,
    TodoRunItem,
    TodoRunState,
    ToolStatePatch,
    goal_from_text,
    goal_label,
    goal_type_value,
    infer_goal_type,
    resolve_turn_intent,
)

__all__ = [
    "Goal",
    "GoalResolution",
    "GoalType",
    "IntentResolution",
    "PendingApproval",
    "TaskState",
    "TodoRunItem",
    "TodoRunState",
    "ToolStatePatch",
    "goal_from_text",
    "goal_label",
    "goal_type_value",
    "infer_goal_type",
    "resolve_turn_intent",
]
