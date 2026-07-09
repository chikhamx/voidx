"""Bash tool — execute shell commands, capture output. Exact, measurable."""

from __future__ import annotations

import asyncio
import os
import subprocess

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.tools.bash.router import try_hint
from voidx.tools.bash.safety import _check_command, _sandbox_denial
from voidx.tools.shell.common import (
    build_blocked_result,
    build_hint_result,
    build_success_result,
    build_timeout_result,
    terminate_process,
)


class BashInput(BaseModel):
    command: str = Field(description="non-interactive Bash command string to execute.")
    timeout: int = Field(default=120, description="Timeout in seconds; the process is terminated if exceeded.")


class BashTool(BaseTool):
    id = "bash"
    description = "Execute a Bash command; working directory is the workspace root. Returns stdout, stderr, and exit code."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(BashInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = BashInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

        blocked = _check_command(inp.command)
        if blocked:
            return build_blocked_result(inp.command, blocked)

        blocked = _sandbox_denial(inp.command, ctx)
        if blocked:
            return build_blocked_result(inp.command, blocked)

        hint = try_hint(inp.command)
        if hint is not None:
            return build_hint_result(inp.command, hint, "Bash")

        try:
            proc = await asyncio.create_subprocess_shell(
                inp.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=ctx.workspace,
                start_new_session=hasattr(os, "killpg"),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=inp.timeout
            )
        except asyncio.TimeoutError:
            await terminate_process(proc)
            return build_timeout_result(inp.command, inp.timeout)

        stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        exit_code = proc.returncode or 0

        return build_success_result(
            inp.command, stdout_text, stderr_text, exit_code, "Bash"
        )
