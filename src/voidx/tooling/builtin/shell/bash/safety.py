"""Map shell execution context values into Bash policy inputs."""

from __future__ import annotations

from voidx.tooling.application.execution import ShellToolContext
from voidx.tooling.builtin.shell.common import terminate_process as _terminate_process
from voidx.tooling.policy.shell.bash import blocked_command_reason
from voidx.tooling.policy.shell.bash_sandbox import sandbox_denial_reason


def check_command(command: str) -> str | None:
    return blocked_command_reason(command)


def sandbox_denial(command: str, ctx: ShellToolContext) -> str | None:
    authorization = ctx.authorization_service
    return sandbox_denial_reason(
        command,
        sandbox_mode=ctx.sandbox_mode,
        workspace=ctx.workspace,
        write_paths=[*authorization.write_files, *authorization.write_dirs],
    )
