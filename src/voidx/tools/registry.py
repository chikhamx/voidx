"""Tool registry — every tool typed, all dispatch quantified."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from voidx.tools.base import ToolContext, ToolResult  # noqa: F401 — re-export
from voidx.tools.file import FileReadTool, ManageTool, WriteTool, FileReplaceTool
from voidx.tools.git import GitTool
from voidx.tools.lsp import LspFormatTool, LspTool
from voidx.tools.search import FindTool, SearchTool
from voidx.tools.bash import BashTool
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoWriteTool
from voidx.tools.skills import SkillsTool
from voidx.tools.web import WebFetchTool, WebSearchTool
from voidx.tools.clarify import ClarifyTool
from voidx.tools.checkpoint import PlanCheckpointTool
from voidx.tools.workflow import WorkflowTool
from voidx.tools.compact import CompactContextTool
from voidx.tools.document import DocumentTool


class ToolDef(BaseModel):
    """A registered tool definition — everything the LLM needs to call it."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    description: str
    parameters: dict  # JSON Schema for the LLM


class ToolRegistry:
    """Manages all available tools. No dynamic discovery — everything explicit."""

    _HIDDEN_FROM_LLM = frozenset({"git"})

    def __init__(self, settings=None, tracker=None) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._instances: dict[str, object] = {}
        self._settings = settings
        self._tracker = tracker
        self._register_builtins()

    def _register_builtins(self) -> None:
        for cls in [
            FileReadTool, ManageTool, WriteTool, FileReplaceTool,
            GitTool,
            FindTool, SearchTool,
            LspTool, LspFormatTool,
            ClarifyTool, PlanCheckpointTool, WorkflowTool, CompactContextTool, DocumentTool,
        ]:
            instance = cls()
            self.register(instance.id, instance, instance.description, instance.parameters_schema())
        # Shell tool — platform-exclusive registration
        import os as _os
        if _os.name == "nt":
            from voidx.tools.powershell import PowerShellTool
            shell_cls = PowerShellTool
        else:
            shell_cls = BashTool
        shell_instance = shell_cls()
        self.register(shell_instance.id, shell_instance, shell_instance.description, shell_instance.parameters_schema())
        # Tools with optional dependency injection
        todo_tool = TodoWriteTool(tracker=self._tracker)
        self.register(todo_tool.id, todo_tool, todo_tool.description, todo_tool.parameters_schema())
        task_status_tool = TaskStatusTool(tracker=self._tracker)
        self.register(task_status_tool.id, task_status_tool, task_status_tool.description, task_status_tool.parameters_schema())
        skills_tool = SkillsTool(settings=self._settings)
        self.register(skills_tool.id, skills_tool, skills_tool.description, skills_tool.parameters_schema())
        retry_config = self._settings.get_retry_config() if self._settings else None
        wf = WebFetchTool(settings=self._settings, retry_config=retry_config)
        self.register(wf.id, wf, wf.description, wf.parameters_schema())
        ws = WebSearchTool(settings=self._settings)
        self.register(ws.id, ws, ws.description, ws.parameters_schema())

    def register(self, tool_id: str, instance: object, description: str, parameters: dict) -> None:
        """Register a tool dynamically (e.g. agent tool injected at runtime)."""
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

    def unregister_prefix(self, prefix: str) -> None:
        for tool_id in [tid for tid in self._tools if tid.startswith(prefix)]:
            self._tools.pop(tool_id, None)
            self._instances.pop(tool_id, None)

    def filter_tools(self, allowed_ids: set[str] | list[str] | tuple[str, ...]) -> None:
        """Retain only the tools listed in allowed_ids."""
        allowed = set(allowed_ids)
        for tool_id in [tid for tid in self._tools if tid not in allowed]:
            self._tools.pop(tool_id, None)
            self._instances.pop(tool_id, None)

    def filtered_copy(self, allowed_ids: set[str] | list[str] | tuple[str, ...]) -> "ToolRegistry":
        """Return a registry view containing existing tool defs and instances."""
        allowed = set(allowed_ids)
        clone = ToolRegistry(settings=self._settings, tracker=self._tracker)
        clone._tools = {
            tool_id: tool_def
            for tool_id, tool_def in self._tools.items()
            if tool_id in allowed
        }
        clone._instances = {
            tool_id: instance
            for tool_id, instance in self._instances.items()
            if tool_id in allowed
        }
        return clone

    def loop_filtered_copy(self, *, workflow_enabled: bool = False) -> "ToolRegistry":
        """Return the closed-world tool view for automatic runtime-backed /loop wakeups."""
        allowed = {
            "read",
            "find",
            "search",
            "lsp",
            "document",
            "websearch",
            "webfetch",
            "mcp",
            "skill",
            "bash",
        }
        if workflow_enabled:
            allowed.update({"workflow", "task_status", "todo"})
        return self.filtered_copy(allowed)

    def ids(self) -> list[str]:
        return list(self._tools.keys())

    def tools_for_llm(self) -> list[dict]:
        """Generate OpenAI/Anthropic-compatible tool definitions."""
        result = []
        for t in self._tools.values():
            if t.id in self._HIDDEN_FROM_LLM:
                continue
            # The MCP gateway passes through third-party arguments, and legacy
            # wrappers may carry non-strict external schemas. Keep MCP non-strict.
            is_mcp = t.id == "mcp" or t.id.startswith("mcp__")
            result.append({
                "type": "function",
                "function": {
                    "name": t.id,
                    "description": t.description,
                    "parameters": t.parameters,
                    **({"strict": True} if not is_mcp else {}),
                },
            })
        return result

    async def execute_tool(self, tool_id: str, args: dict, ctx: ToolContext) -> ToolResult:
        """Execute a tool by id. Returns typed result."""
        tool = self.get(tool_id)
        if not tool:
            return ToolResult(output=f"Unknown tool: {tool_id}. Available: {self.ids()}")
        return await tool.execute(args, ctx)
