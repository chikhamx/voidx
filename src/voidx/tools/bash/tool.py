"""Bash tool — execute shell commands, capture output. Exact, measurable."""

from __future__ import annotations

import asyncio
import os
import subprocess

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.tools.bash.edit_router import maybe_route_sed_edit
from voidx.tools.bash.router import try_hint
from voidx.permission.context import PermissionContext
from voidx.permission.grants import AccessGrants
from voidx.permission.shell_policy import shell_sandbox_precheck
from voidx.tools.bash.safety import _check_command, _sandbox_denial
from voidx.tools.shell.common import (
    build_blocked_result,
    build_hint_result,
    build_success_result,
    build_timeout_result,
    maybe_route_hint,
    create_owned_subprocess_shell,
    release_owned_process,
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

        approved_shell_risk = ctx.has_approved_tool_risk("bash", inp.command)
        blocked = None if approved_shell_risk else _sandbox_denial(inp.command, ctx)
        if blocked:
            return build_blocked_result(inp.command, blocked)

        sed_routed = await maybe_route_sed_edit(inp.command, ctx, "bash")
        if sed_routed is not None:
            return sed_routed

        hint = try_hint(inp.command)
        if hint is not None:
            routed = await maybe_route_hint(inp.command, hint, ctx, "bash")
            if routed is not None:
                return routed
            return build_hint_result(inp.command, hint, "Bash")
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
                    permission_mode=ctx.permission_mode,
                    access_grants=access_grants,
                    process_sandbox=ctx.process_sandbox,
                ),
                shell="bash",
            )
        if shell_blocked:
            return build_blocked_result(inp.command, shell_blocked)

        proc = await create_owned_subprocess_shell(
            inp.command,
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

        stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        exit_code = proc.returncode or 0

        return build_success_result(
            inp.command, stdout_text, stderr_text, exit_code, "Bash"
        )
