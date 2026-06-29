"""PowerShell tool — execute commands on Windows, capture output. Exact, measurable."""

from __future__ import annotations

import asyncio
import os
import subprocess

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.tools.powershell.safety import _check_command
from voidx.tools.powershell.sandbox import _sandbox_denial
from voidx.tools.powershell.router import try_hint as _try_hint
from voidx.tools.shell.common import (
    build_blocked_result,
    build_hint_result,
    build_success_result,
    build_timeout_result,
    terminate_process,
)


class PowerShellInput(BaseModel):
    command: str = Field(description="PowerShell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds")


class PowerShellTool(BaseTool):
    id = "powershell"
    description = "Execute a PowerShell command in the workspace directory on Windows. Returns stdout, stderr, and exit code."

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

        sandbox_blocked = _sandbox_denial(inp.command, ctx)
        if sandbox_blocked:
            return build_blocked_result(inp.command, sandbox_blocked)

        hint = _try_hint(inp.command)
        if hint is not None:
            return build_hint_result(inp.command, hint, "PowerShell")

        try:
            proc = await asyncio.create_subprocess_exec(
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
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=inp.timeout
            )
        except asyncio.TimeoutError:
            await terminate_process(proc)
            return build_timeout_result(inp.command, inp.timeout)
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
