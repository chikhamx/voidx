"""Task state shim — compat stubs."""

from dataclasses import dataclass, field


@dataclass
class TaskRun:
    intent: str = ""
    approved: bool = False
    goal: str = ""
    goal_phase: str = ""
    goal_status: str = ""
    goal_turn_count: int = 0


@dataclass
class TaskState:
    runs: list[TaskRun] = field(default_factory=list)
    current: TaskRun | None = None
