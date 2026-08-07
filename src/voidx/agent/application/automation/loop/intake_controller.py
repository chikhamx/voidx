"""Intake-scoped controller for initializing a LoopSpec."""

from __future__ import annotations

from voidx.agent.domain.automation.loop import LoopSpec


class LoopIntakeController:
    def __init__(self) -> None:
        self._spec: LoopSpec | None = None
        self._cancelled = False

    async def submit_init(self, spec: LoopSpec) -> LoopSpec:
        if self._spec is None:
            self._spec = spec
        return self._spec

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def final_spec(self) -> LoopSpec | None:
        return self._spec
