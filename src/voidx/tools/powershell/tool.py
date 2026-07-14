"""PowerShell tool — execute commands on Windows, capture output. Exact, measurable."""

from __future__ import annotations

import asyncio
import os
import subprocess

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.permission.context import PermissionContext
from voidx.permission.grants import AccessGrants
from voidx.permission.shell_policy import shell_sandbox_precheck
from voidx.tools.powershell.safety import _check_command
from voidx.tools.powershell.sandbox import _sandbox_denial
from voidx.tools.powershell.router import try_hint as _try_hint
from voidx.tools.shell.common import (
    build_blocked_result,
    build_hint_result,
    build_success_result,
    build_timeout_result,
    create_owned_subprocess_exec,
    release_owned_process,
    terminate_process,
)


class PowerShellInput(BaseModel):
    command: str = Field(description="PowerShell command string to execute non-interactively.")
    timeout: int = Field(default=120, description="Timeout in seconds; the process is terminated if exceeded.")


class PowerShellTool(BaseTool):
    id = "powershell"
    description = "Execute a PowerShell command on Windows only; working directory is the workspace root. Returns stdout, stderr, and exit code."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(PowerShellInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = PowerShellInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

        blocked = _check_command(inp.command)
        if blocked:
            return build_blocked_result(inp.command, blocked)

        approved_shell_risk = ctx.has_approved_tool_risk("powershell", inp.command)
        sandbox_blocked = None if approved_shell_risk else _sandbox_denial(inp.command, ctx)
        if sandbox_blocked:
            return build_blocked_result(inp.command, sandbox_blocked)

        hint = _try_hint(inp.command)
        if hint is not None:
            return build_hint_result(inp.command, hint, "PowerShell")
        access_grants = ctx.get_access_grants() if ctx.get_access_grants is not None else AccessGrants.from_parts(
            readable_files=ctx.sandbox_readable_files,
            readable_dirs=ctx.sandbox_readable_dirs,
            writable_files=ctx.sandbox_writable_files,
            writable_dirs=ctx.sandbox_writable_dirs,
        )
        shell_blocked = None
        if not approved_shell_risk:
            _, shell_blocked = shell_sandbox_precheck(
                {"command": inp.command},
                PermissionContext(
                    workspace=ctx.workspace,
                    sandbox_mode=ctx.sandbox_mode,
                    access_grants=access_grants,
                    process_sandbox=ctx.process_sandbox,
                ),
                shell="powershell",
            )
        if shell_blocked:
            return build_blocked_result(inp.command, shell_blocked)

        try:
            proc = await create_owned_subprocess_exec(
                _powershell_exe(),
                "-NoProfile",
                "-NonInteractive",
                "-OutputFormat", "Text",
                "-Command",
                f"$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; {inp.command}",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=ctx.workspace,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=inp.timeout
                )
                await release_owned_process(proc)
            except asyncio.TimeoutError:
                await terminate_process(proc)
                return build_timeout_result(inp.command, inp.timeout)
            except asyncio.CancelledError:
                await terminate_process(proc)
                raise
        except FileNotFoundError:
            return ToolResult(
                output="powershell.exe not found. Check your Windows installation.",
                display="powershell.exe not found. Check your Windows installation.",
                metadata={"command": inp.command, "error": True},
            )

        stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        exit_code = proc.returncode or 0

        return build_success_result(
            inp.command, stdout_text, stderr_text, exit_code, "PowerShell"
        )


def _powershell_exe() -> str:
    """Locate powershell.exe, falling back to a full path if not on PATH."""
    import shutil
    found = shutil.which("powershell")
    if found:
        return found
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
