"""Permission-aware tool execution use case."""

from voidx.agent.ports.permission import PermissionAuthorizer
from voidx.agent.ports.tools import ToolExecutionResult, ToolExecutor


class ToolService:
    def __init__(self, permission: PermissionAuthorizer, tools: ToolExecutor) -> None:
        self._permission = permission
        self._tools = tools

    async def execute(self, tool_name: str, arguments: dict) -> ToolExecutionResult:
        if not await self._permission.authorize(tool_name, arguments):
            return ToolExecutionResult(
                output=f"Permission denied for tool: {tool_name}",
                denied=True,
                metadata={"error": True, "error_kind": "permission_denied"},
            )
        return await self._tools.execute(tool_name, arguments)
