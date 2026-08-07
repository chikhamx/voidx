"""Tool plugin contract consumed by Tooling application services."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.result import ToolResult


class ToolPlugin(Protocol):
    id: str
    description: str

    def parameters_schema(self) -> dict[str, Any]: ...
    async def execute(self, args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult: ...




__all__ = ["ToolPlugin"]
