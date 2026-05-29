"""Tool registry — every tool typed, all dispatch quantified."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from voidx.tools.base import ToolContext, ToolResult  # noqa: F401 — re-export
from voidx.tools.file_ops import FileReadTool, FileWriteTool, FileEditTool
from voidx.tools.repomap import RepoMapTool
from voidx.tools.search import GlobTool, GrepTool
from voidx.tools.bash import BashTool
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoWriteTool
from voidx.tools.webfetch import WebFetchTool
from voidx.tools.websearch import WebSearchTool


class ToolDef(BaseModel):
    """A registered tool definition — everything the LLM needs to call it."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    description: str
    parameters: dict  # JSON Schema for the LLM


class ToolRegistry:
    """Manages all available tools. No dynamic discovery — everything explicit."""

    def __init__(self, settings=None) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._instances: dict[str, object] = {}
        self._settings = settings
        self._register_builtins()

    def _register_builtins(self) -> None:
        for cls in [FileReadTool, FileWriteTool, FileEditTool,
                     RepoMapTool,
                     GlobTool, GrepTool, BashTool,
                     TodoWriteTool, WebFetchTool]:
            instance = cls()
            self.register(instance.id, instance, instance.description, instance.parameters_schema())
        # WebSearchTool needs settings for Tavily API key
        ws = WebSearchTool(settings=self._settings)
        self.register(ws.id, ws, ws.description, ws.parameters_schema())

    def register(self, tool_id: str, instance: object, description: str, parameters: dict) -> None:
        """Register a tool dynamically (e.g. task tool injected at runtime)."""
        self._tools[tool_id] = ToolDef(
            id=tool_id, description=description, parameters=parameters,
        )
        self._instances[tool_id] = instance

    def list(self) -> list[ToolDef]:
        return list(self._tools.values())

    def get(self, tool_id: str):
        return self._instances.get(tool_id)

    def get_def(self, tool_id: str) -> ToolDef | None:
        return self._tools.get(tool_id)

    def ids(self) -> list[str]:
        return list(self._tools.keys())

    def tools_for_llm(self) -> list[dict]:
        """Generate OpenAI/Anthropic-compatible tool definitions."""
        result = []
        for t in self._tools.values():
            result.append({
                "type": "function",
                "function": {
                    "name": t.id,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            })
        return result

    async def execute_tool(self, tool_id: str, args: dict, ctx: ToolContext) -> ToolResult:
        """Execute a tool by id. Returns typed result."""
        tool = self.get(tool_id)
        if not tool:
            return ToolResult(output=f"Unknown tool: {tool_id}. Available: {self.ids()}")
        return await tool.execute(args, ctx)
