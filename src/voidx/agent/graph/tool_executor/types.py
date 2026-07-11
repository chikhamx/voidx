from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.messages import ToolMessage

from voidx.agent.task_state import GoalSpec, TaskState, TodoRunState


@dataclass
class _ExecutedTool:
    message: ToolMessage | None
    result: object
    tool_call: dict
    todo_state: TodoRunState | None = None
    terminal_reason: str | None = None
    runtime_guard_eligible: bool = True


ToolResultOk = Callable[[object], bool]

AGENT_RESULT_PREVIEW_LINES = 5
AGENT_RESULT_PREVIEW_CHARS = 1200


def _tool_result_ok(result) -> bool:
    metadata = getattr(result, "metadata", {}) or {}
    if metadata.get("error") or metadata.get("blocked") or metadata.get("timeout"):
        return False
    if "exit_code" in metadata:
        try:
            return int(metadata.get("exit_code") or 0) == 0
        except (TypeError, ValueError):
            return False
    return True


def _task_state_for_state(value: object, fallback: TaskState | None = None) -> TaskState:
    if isinstance(value, TaskState):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        try:
            return TaskState.model_validate(value)
        except ValueError:
            pass
    if fallback is not None:
        return fallback.model_copy(deep=True)
    return TaskState()


def _goal_for_state(value: object | None) -> GoalSpec | None:
    if value is None:
        return None
    if isinstance(value, GoalSpec):
        return value
    if isinstance(value, dict):
        try:
            return GoalSpec.model_validate(value)
        except ValueError:
            return None
    return None


def _todo_state_for_state(value: object | None) -> TodoRunState | None:
    if value is None:
        return None
    if isinstance(value, TodoRunState):
        return value
    if isinstance(value, dict):
        try:
            return TodoRunState.model_validate(value)
        except ValueError:
            return None
    return None


def _workflow_runs_for_state(value: object) -> list:
    from voidx.workflow.types import WorkflowRunState
    runs: list[WorkflowRunState] = []
    items = value.values() if isinstance(value, dict) else value or []
    for item in items:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        runs.append(run)
    return runs
