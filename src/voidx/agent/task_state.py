"""Compatibility exports for task intent state."""

from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
    TaskState,
    TodoRunItem,
    TodoRunState,
    ToolStatePatch,
    TurnExchange,
    WorkflowRoute,
    goal_label,
    goal_type_value,
    infer_goal_type,
)

__all__ = [
    "GoalSpec",
    "GoalResolution",
    "GoalType",
    "IntentResolution",
    "PlanResolution",
    "TaskState",
    "TodoRunItem",
    "TodoRunState",
    "ToolStatePatch",
    "TurnExchange",
    "WorkflowRoute",
    "goal_label",
    "goal_type_value",
    "infer_goal_type",
]
