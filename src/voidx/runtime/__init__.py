"""Runtime contracts shared by agent, tools, and persistence."""

from voidx.runtime.intent import InteractionMode, TaskIntent
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
from voidx.runtime.ui import (
    AgentUiSink,
    NoOpAgentUiSink,
    reset_ui_sink,
    set_ui_sink,
    use_noop_ui_sink,
)

__all__ = [
    "AgentUiSink",
    "NoOpAgentUiSink",
    "InteractionMode",
    "TaskIntent",
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
    "reset_ui_sink",
    "set_ui_sink",
    "use_noop_ui_sink",
]
