"""Task tracker — shared state for running worker-role status."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

TaskStatus = Literal["pending", "running", "completed", "error", "cancelled"]


@dataclass
class TaskState:
    id: str
    agent: str
    description: str
    status: TaskStatus = "pending"
    step: int = 0
    max_steps: int = 0
    last_output: str = ""  # most recent text preview
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class TaskTracker:
    """Thread-safe registry for running worker-role tasks."""

    def __init__(self):
        self._tasks: dict[str, TaskState] = {}
        self._todos: list = []  # type: ignore[type-arg]

    def start(self, task_id: str, agent: str, description: str, max_steps: int = 25) -> TaskState:
        state = TaskState(
            id=task_id, agent=agent, description=description,
            status="running", max_steps=max_steps,
        )
        self._tasks[task_id] = state
        return state

    def update(self, task_id: str, **kwargs):
        if task_id in self._tasks:
            for k, v in kwargs.items():
                if hasattr(self._tasks[task_id], k):
                    setattr(self._tasks[task_id], k, v)
            self._tasks[task_id].updated_at = time.time()

    def finish(self, task_id: str, status: TaskStatus = "completed"):
        if task_id in self._tasks:
            self._tasks[task_id].status = status
            self._tasks[task_id].updated_at = time.time()

    def get(self, task_id: str) -> TaskState | None:
        return self._tasks.get(task_id)

    def list_all(self) -> list[TaskState]:
        return list(self._tasks.values())

    def list_running(self) -> list[TaskState]:
        return [t for t in self._tasks.values() if t.status in ("pending", "running")]

    def remove(self, task_id: str):
        self._tasks.pop(task_id, None)

    def format_status(self) -> str:
        """Format all tasks as a status report string."""
        tasks = self.list_all()
        if not tasks:
            return "No worker-role tasks."

        lines = []
        for t in tasks:
            icon = {"pending": "○", "running": "◐", "completed": "●", "error": "✕", "cancelled": "◌"}[t.status]
            elapsed = int(time.time() - t.created_at)
            step_info = f"step {t.step}/{t.max_steps}" if t.max_steps else ""
            preview = t.last_output[:100] if t.last_output else ""
            lines.append(
                f"{icon} [{t.id}] {t.agent} — {t.status} ({step_info}, {elapsed}s)\n"
                f"   {t.description[:120]}\n"
                + (f"   last: {preview}" if preview else "")
            )
        return "\n".join(lines)
