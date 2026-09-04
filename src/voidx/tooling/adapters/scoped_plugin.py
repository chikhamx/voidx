"""Explicit adapters that inject narrow Tooling services into plugins."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    def clone_for_child(self) -> "FileScopedPlugin":
        """New wrapper instance for a child run; the child rebinds scoped services."""
        tool = self.tool
        clone_for_child = getattr(tool, "clone_for_child", None)
        if callable(clone_for_child):
            tool = clone_for_child()
        return replace(self, tool=tool)



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

    def as_read_only(self) -> "ShellScopedPlugin":
        return ReadOnlyShellPlugin(
            tool=self.tool,
            authorization=self.authorization,
            files=self.files,
            formatter=self.formatter,
            process_sandbox=self.process_sandbox,
            invoker=self.invoker,
        )


class ReadOnlyShellPlugin(ShellScopedPlugin):
    """Shell plugin that hard-rejects anything beyond a classified safe read.

    Used for debug-mode child agents: the shell stays visible for read-only
    verification commands, but the guard — not the model — enforces the
    boundary, so forged or speculative write commands never execute.
    """

    def as_read_only(self) -> "ReadOnlyShellPlugin":
        return self

    async def execute(self, args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        from voidx.tooling.domain.risk import RiskLevel, RiskTag
        from voidx.tooling.policy.shell.policy import classify_shell_risk

        command = str(args.get("command", "")) if isinstance(args, dict) else ""
        shell = "powershell" if self.id == "powershell" else "bash"
        risk = classify_shell_risk(command, shell=shell, workspace=getattr(ctx, "workspace", None))
        if risk.level is not RiskLevel.NORMAL or set(risk.tags) - {RiskTag.SAFE_READ}:
            return ToolResult(
                output=(
                    "Command rejected: this debug agent may only run read-only shell "
                    f"commands (classified: {risk.reason}). Use read/search/lsp tools instead."
                ),
                metadata={"error": True, "reason": "read_only_shell", "command": command[:120]},
            )
        return await super().execute(args, ctx)



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
