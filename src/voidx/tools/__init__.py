"""voidx tools — typed tool system with registry dispatch."""

from voidx.tools.base import BaseTool, ToolContext, ToolResult, resolve_safe


def __getattr__(name: str):
    if name == "ToolRegistry":
        from voidx.tools.registry import ToolRegistry

        return ToolRegistry
    raise AttributeError(name)

__all__ = [
    "BaseTool",
    "ToolContext",
    "ToolResult",
    "ToolRegistry",
    "resolve_safe",
]
