"""Attempt-scoped controller for durable Goal work checkpoints."""

from __future__ import annotations

from voidx.agent.domain.automation.goal import WorkCheckpoint


class GoalCheckpointController:
    def __init__(self, *, attempt_id: str = "") -> None:
        self.attempt_id = attempt_id
        self._checkpoint: WorkCheckpoint | None = None
        self._protocol_id = ""

    async def submit_checkpoint(
        self,
        checkpoint: WorkCheckpoint,
        *,
        protocol_id: str = "",
    ) -> WorkCheckpoint:
        if self._checkpoint is None:
            self._checkpoint = checkpoint
            self._protocol_id = protocol_id.strip()
        return self._checkpoint

    def final_checkpoint(self) -> WorkCheckpoint | None:
        return self._checkpoint

    def final_protocol_id(self) -> str:
        return self._protocol_id
