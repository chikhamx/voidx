"""MCP server configuration DTO."""

from __future__ import annotations

from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    name: str
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = Field(
        default=None,
        description="Working directory for the subprocess. Inherited from parent if not set.",
    )
    headers: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    disabled: bool = False
    auto: bool = False
    description: str = ""
    source: str = ""
    tools: list[str] | dict[str, object] | None = None
    transport: str = ""

    @property
    def effective_transport(self) -> str:
        if self.transport:
            return self.transport
        if self.url:
            return "sse"
        return "stdio"

    @property
    def tool_count(self) -> int:
        if isinstance(self.tools, dict):
            return len(self.tools)
        if isinstance(self.tools, list):
            return len(self.tools)
        return 0


__all__ = ["McpServerConfig"]
