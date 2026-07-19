"""Tool execution port and application-facing result."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class ToolExecutionResult(BaseModel):
    output: str
    denied: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class ToolExecutor(Protocol):
    async def execute(self, tool_name: str, arguments: dict) -> ToolExecutionResult: ...
