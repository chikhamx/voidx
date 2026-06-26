"""Bash tool — execute shell commands, capture output. Exact, measurable."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.tools.bash.router import try_hint
from voidx.tools.bash.safety import _check_command, _sandbox_denial, _terminate_process


class BashInput(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds")


class BashTool(BaseTool):
    id = "bash"
    description = "Execute a shell command in the workspace directory. Returns stdout, stderr, and exit code."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(BashInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = BashInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

        blocked = _check_command(inp.command)
        if blocked:
            payload = {"ok": False, "exit_code": -1, "stdout": "", "stderr": blocked, "blocked": True}
            return ToolResult(
                output=json.dumps(payload, ensure_ascii=False),
                display=blocked,
                metadata={"command": inp.command, "blocked": True, "error": True},
            )

        blocked = _sandbox_denial(inp.command, ctx)
        if blocked:
            payload = {"ok": False, "exit_code": -1, "stdout": "", "stderr": blocked, "blocked": True}
            return ToolResult(
                output=json.dumps(payload, ensure_ascii=False),
                display=blocked,
                metadata={"command": inp.command, "blocked": True, "error": True},
            )

        hint = try_hint(inp.command)
        if hint is not None:
            return ToolResult(
                title=f"Bash route hint: {inp.command}",
                output=(
                    f"[{hint.ui_label}]\n"
                    "Command not executed because a specialized tool is available."
                ),
                summary="route hint",
                metadata={
                    "command": inp.command,
                    "skipped": True,
                    "route_hint": {"tool_id": hint.tool_id, "command": inp.command},
                },
                next_step_hint=hint.llm_hint,
            )

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
            await _terminate_process(proc)
            payload = {"ok": False, "exit_code": -1, "stdout": "", "stderr": "", "timeout": True}
            display = f"Command timed out after {inp.timeout}s: {inp.command}"
            return ToolResult(
                output=json.dumps(payload, ensure_ascii=False),
                display=display,
                metadata={"command": inp.command, "exit_code": -1, "timeout": True, "error": True},
            )

        stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        exit_code = proc.returncode or 0

        display_parts = []
        if stdout_text:
            display_parts.append(stdout_text)
        if stderr_text:
            display_parts.append(f"[stderr]\n{stderr_text}")
        if exit_code != 0 and not stdout_text and not stderr_text:
            display_parts.append(
                "Interactive commands that read from stdin are not supported. "
                "Use non-interactive flags or pipe input via echo/heredoc."
            )

        payload = {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

        result = ToolResult(
            title=f"Bash: {inp.command}",
            output=json.dumps(payload, ensure_ascii=False),
            display="\n".join(display_parts) or "(no output)",
            summary=f"exit {exit_code}",
            metadata={
                "command": inp.command,
                "exit_code": exit_code,
                "ok": exit_code == 0,
                **({"error": True} if exit_code != 0 else {}),
            },
        )

        return result
