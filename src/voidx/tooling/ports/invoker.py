"""Narrow port for invoking another registered tool."""

from __future__ import annotations

from typing import Protocol

from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.result import ToolResult


class ToolInvoker(Protocol):
    def get(self, tool_id: str) -> object | None: ...

    async def execute_tool(
        self,
        tool_id: str,
        args: dict,
        ctx: ToolExecutionContext,
    ) -> ToolResult: ...


__all__ = ["ToolInvoker"]
