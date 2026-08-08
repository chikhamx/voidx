"""Bash tool — execute shell commands, capture output. Exact, measurable."""

from __future__ import annotations

import asyncio
import os
import subprocess

from pydantic import BaseModel, Field

from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.schema import model_to_json_schema
from voidx.tooling.builtin.shell.bash.edit_router import maybe_route_sed_edit
from voidx.tooling.builtin.shell.bash.router import try_hint
from voidx.tooling.domain.authorization import PermissionContext
from voidx.tooling.domain.grants import AccessGrants
from voidx.tooling.policy.shell.policy import shell_sandbox_precheck
from voidx.tooling.builtin.shell.bash.safety import check_command, sandbox_denial
from voidx.tooling.builtin.shell.common import (
    build_blocked_result,
    build_hint_result,
    build_success_result,
    build_timeout_result,
    maybe_route_hint,
    resolve_shell_workspace,
    create_owned_subprocess_shell,
    release_owned_process,
    terminate_process,
)


class BashInput(BaseModel):
    command: str = Field(description="non-interactive Bash command string to execute.")
    timeout: int = Field(default=120, description="Timeout in seconds; the process is terminated if exceeded.")


class BashTool:
    id = "bash"
    description = "Execute a Bash command; working directory is the workspace root. Returns stdout, stderr, and exit code."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(BashInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = BashInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

        blocked = check_command(inp.command)
        if blocked:
            return build_blocked_result(inp.command, blocked)

        workspace, workspace_error = resolve_shell_workspace(inp.command, ctx.workspace)
        if workspace_error is not None:
            return workspace_error

        approved_shell_risk = ctx.has_approved_tool_risk("bash", inp.command)
        blocked = None if approved_shell_risk else sandbox_denial(inp.command, ctx)
        if blocked:
            return build_blocked_result(inp.command, blocked)

        sed_routed = await maybe_route_sed_edit(inp.command, ctx, "bash")
        if sed_routed is not None:
            return sed_routed

        hint = try_hint(inp.command)
        git_fallback = False
        if hint is not None:
            routed = await maybe_route_hint(inp.command, hint, ctx, "bash")
            if routed is not None:
                return routed
            if hint.tool_id != "git":
                return build_hint_result(inp.command, hint, "Bash")
            git_fallback = True
        access_grants = ctx.authorization_service.access_grants() if ctx.authorization_service.access_grants is not None else AccessGrants.from_parts(
            readable_files=ctx.authorization_service.read_files,
            readable_dirs=ctx.authorization_service.read_dirs,
            writable_files=ctx.authorization_service.write_files,
            writable_dirs=ctx.authorization_service.write_dirs,
        )
        shell_blocked = None
        if not approved_shell_risk and not git_fallback:
            _, shell_blocked = shell_sandbox_precheck(
                {"command": inp.command},
                PermissionContext(
                    workspace=workspace,
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
            cwd=workspace,
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
