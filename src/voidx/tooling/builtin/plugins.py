"""Explicit factory for Tooling built-in plugins."""

from __future__ import annotations

import os
from typing import Any

from voidx.tooling.application.execution import AuthorizationRuntime
from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.adapters.scoped_plugin import FileScopedPlugin, ShellScopedPlugin
from voidx.tooling.builtin.document import DocumentTool
from voidx.tooling.builtin.file import FileReadTool, FileReplaceTool, ManageTool, WriteTool
from voidx.tooling.builtin.file.search import FindTool, SearchTool
from voidx.tooling.builtin.git import GitTool
from voidx.tooling.builtin.shell.bash import BashTool
from voidx.tooling.builtin.shell.powershell import PowerShellTool
from voidx.tooling.ports.post_edit import PostEditFormatter
from voidx.tooling.ports.invoker import ToolInvoker
from voidx.tooling.ports.process import ProcessSandbox
from voidx.tooling.ports.tool import ToolPlugin


def build_builtin_plugins(
    *,
    authorization: AuthorizationRuntime | None = None,
    files: FileStateStore | None = None,
    process_sandbox: ProcessSandbox | None = None,
    invoker: ToolInvoker | None = None,
    formatter: PostEditFormatter | None = None,
) -> list[ToolPlugin]:
    authorization = authorization or AuthorizationRuntime()
    files = files or FileStateStore()
    file_tools = [
        FileReadTool(),
        ManageTool(),
        WriteTool(),
        FileReplaceTool(),
        GitTool(),
        FindTool(),
        SearchTool(),
    ]
    shell_type: Any = PowerShellTool if os.name == "nt" else BashTool
    plugins: list[ToolPlugin] = [
        *(FileScopedPlugin(tool, authorization, files, formatter) for tool in file_tools),
        ShellScopedPlugin(
            shell_type(),
            authorization,
            files,
            process_sandbox=process_sandbox,
            invoker=invoker,
        ),
        DocumentTool(),
    ]
    return plugins


__all__ = ["build_builtin_plugins"]
