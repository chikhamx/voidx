"""Compatibility exports for task intent state."""

from voidx.runtime.task_state import (
    IntentResolution,
    PendingApproval,
    TaskPhase,
    TaskRun,
    TaskRunStatus,
    TaskState,
    TodoRunItem,
    TodoRunState,
    ToolStatePatch,
    resolve_turn_intent,
)

__all__ = [
    "IntentResolution",
    "PendingApproval",
    "TaskPhase",
    "TaskRun",
    "TaskRunStatus",
    "TaskState",
    "TodoRunItem",
    "TodoRunState",
    "ToolStatePatch",
    "resolve_turn_intent",
]
