"""Workflow command port."""

from typing import Protocol


class WorkflowController(Protocol):
    async def dispatch(self, action: str, arguments: dict) -> dict: ...
