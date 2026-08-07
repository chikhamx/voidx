"""Intake-scoped controller for initializing a GoalSpec."""

from __future__ import annotations

from voidx.agent.domain.automation.goal import GoalSpec


class GoalIntakeController:
    def __init__(self) -> None:
        self._spec: GoalSpec | None = None
        self._cancelled = False

    async def submit_init(self, spec: GoalSpec) -> GoalSpec:
        if self._spec is None:
            self._spec = spec
        return self._spec

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def final_spec(self) -> GoalSpec | None:
        return self._spec
