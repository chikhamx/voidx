"""Tool registry — explicit plugin catalog and dispatch."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.ports.tool import ToolPlugin


class ToolDef(BaseModel):
    """A registered tool definition — everything the LLM needs to call it."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    description: str
    parameters: dict


class ToolRegistry:
    """Manage an explicitly supplied catalog of tool plugins."""

    def __init__(self, plugins: Iterable[ToolPlugin] = ()) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._instances: dict[str, ToolPlugin] = {}
        for plugin in plugins:
            self.register_plugin(plugin)

    def register_plugin(self, plugin: ToolPlugin) -> None:
        """Register a plugin and reject duplicate ids in a catalog build."""
        if plugin.id in self._instances:
            raise ValueError(f"Duplicate tool id: {plugin.id}")
        self.register(plugin.id, plugin, plugin.description, plugin.parameters_schema())

    def register(self, tool_id: str, instance: ToolPlugin, description: str, parameters: dict) -> None:
        """Register a runtime-bound tool instance and reject duplicate ids."""
        if tool_id in self._instances:
            raise ValueError(f"Duplicate tool id: {tool_id}")
        self._tools[tool_id] = ToolDef(id=tool_id, description=description, parameters=parameters)
        self._instances[tool_id] = instance

    def replace(self, tool_id: str, instance: ToolPlugin, description: str, parameters: dict) -> None:
        """Explicitly replace an existing runtime binding without changing catalog order."""
        if tool_id not in self._instances:
            raise KeyError(f"Unknown tool id: {tool_id}")
        self._tools[tool_id] = ToolDef(id=tool_id, description=description, parameters=parameters)
        self._instances[tool_id] = instance


    def list(self) -> list[ToolDef]:
        return list(self._tools.values())

    def get(self, tool_id: str) -> ToolPlugin | None:
        return self._instances.get(tool_id)

    def get_def(self, tool_id: str) -> ToolDef | None:
        return self._tools.get(tool_id)

    def unregister_prefix(self, prefix: str) -> None:
        for tool_id in [tid for tid in self._tools if tid.startswith(prefix)]:
            self._tools.pop(tool_id, None)
            self._instances.pop(tool_id, None)

    def filter_tools(self, allowed_ids: set[str] | list[str] | tuple[str, ...]) -> None:
        allowed = set(allowed_ids)
        for tool_id in [tid for tid in self._tools if tid not in allowed]:
            self._tools.pop(tool_id, None)
            self._instances.pop(tool_id, None)

    def filtered_copy(self, allowed_ids: set[str] | list[str] | tuple[str, ...]) -> "ToolRegistry":
        allowed = set(allowed_ids)
        clone = ToolRegistry()
        clone._tools = {tool_id: tool_def for tool_id, tool_def in self._tools.items() if tool_id in allowed}
        clone._instances = {tool_id: instance for tool_id, instance in self._instances.items() if tool_id in allowed}
        return clone

    def loop_filtered_copy(self, *, workflow_enabled: bool = False) -> "ToolRegistry":
        allowed = {
            "read", "find", "search", "lsp", "document", "websearch", "webfetch",
            "mcp", "skill", "bash", "loop",
        }
        if workflow_enabled:
            allowed.update({"workflow", "todo"})
        return self.filtered_copy(allowed)

    def ids(self) -> list[str]:
        return list(self._tools.keys())

    def serialize_definitions(self) -> list[dict[str, Any]]:
        result = []
        for tool in self._tools.values():
            result.append({
                "type": "function",
                "function": {
                    "name": tool.id,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    **({"strict": True} if tool.id != "mcp" else {}),
                },
            })
        return result

    def tools_for_llm(self) -> list[dict[str, Any]]:
        """Return the complete catalog; visibility filtering belongs to the caller."""
        return self.serialize_definitions()

    async def execute_tool(self, tool_id: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self.get(tool_id)
        if not tool:
            return ToolResult(output=f"Unknown tool: {tool_id}. Available: {self.ids()}")
        return await tool.execute(args, ctx)
