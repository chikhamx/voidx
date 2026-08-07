"""Explicit adapters that inject narrow Tooling services into plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from voidx.tooling.application.execution import (
    AuthorizationRuntime,
    FileToolContext,
    ShellToolContext,
)
from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.ports.post_edit import PostEditFormatter
from voidx.tooling.ports.invoker import ToolInvoker
from voidx.tooling.ports.process import ProcessSandbox


@dataclass
class FileScopedPlugin:
    tool: Any
    authorization: AuthorizationRuntime
    files: FileStateStore
    formatter: PostEditFormatter | None = None

    @property
    def id(self) -> str:
        return self.tool.id

    @property
    def description(self) -> str:
        return self.tool.description

    def parameters_schema(self) -> dict[str, Any]:
        return self.tool.parameters_schema()

    async def execute(self, args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        if isinstance(ctx, FileToolContext):
            scoped = (
                ctx.model_copy(update={"post_edit_formatter": self.formatter})
                if ctx.post_edit_formatter is None
                else ctx
            )
            return await self.tool.execute(args, scoped)
        scoped = FileToolContext(
            **ctx.model_dump(),
            authorization_service=self.authorization,
            file_state=self.files,
            post_edit_formatter=self.formatter,
        )
        return await self.tool.execute(args, scoped)


@dataclass
class ShellScopedPlugin(FileScopedPlugin):
    process_sandbox: ProcessSandbox | None = None
    invoker: ToolInvoker | None = None

    async def execute(self, args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        if isinstance(ctx, ShellToolContext):
            updates = {}
            if ctx.post_edit_formatter is None:
                updates["post_edit_formatter"] = self.formatter
            if ctx.process_sandbox is None:
                updates["process_sandbox"] = self.process_sandbox
            if ctx.tool_invoker is None:
                updates["tool_invoker"] = self.invoker
            scoped = ctx.model_copy(update=updates) if updates else ctx
            return await self.tool.execute(args, scoped)
        scoped = ShellToolContext(
            **ctx.model_dump(),
            authorization_service=self.authorization,
            file_state=self.files,
            post_edit_formatter=self.formatter,
            process_sandbox=self.process_sandbox,
            tool_invoker=self.invoker,
        )
        return await self.tool.execute(args, scoped)


def bind_scoped_plugins(
    registry: Any,
    *,
    authorization: AuthorizationRuntime,
    files: FileStateStore,
    process_sandbox: ProcessSandbox | None = None,
    formatter: PostEditFormatter | None = None,
) -> None:
    if not hasattr(registry, "list") or not hasattr(registry, "get"):
        return
    for tool_def in registry.list():
        plugin = registry.get(tool_def.id)
        if isinstance(plugin, FileScopedPlugin):
            plugin.authorization = authorization
            plugin.files = files
            plugin.formatter = formatter
        if isinstance(plugin, ShellScopedPlugin):
            plugin.process_sandbox = process_sandbox
            plugin.invoker = registry


__all__ = ["FileScopedPlugin", "ShellScopedPlugin", "bind_scoped_plugins"]
