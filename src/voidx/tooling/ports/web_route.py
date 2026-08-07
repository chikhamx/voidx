"""Optional routing of built-in web calls to an integration backend."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.result import ToolResult


class WebRoute(Protocol):
    async def __call__(
        self,
        *,
        kind: str,
        settings: Any,
        ctx: ToolExecutionContext,
        arguments: dict[str, Any],
        title: str,
    ) -> ToolResult | None: ...


__all__ = ["WebRoute"]
