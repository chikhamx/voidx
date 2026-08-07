"""Git subprocess execution primitives."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from voidx.platform.processes import (
    create_owned_subprocess_exec,
    finalize_process_tree,
    release_owned_process,
)
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import tool_timeout_metadata

from voidx.tooling.builtin.git.constants import GIT_TIMEOUT_SECONDS
from voidx.tooling.builtin.git.models import GitProcessTimeout, GitRepo


async def run_process(args: list[str], *, cwd: str, read_only: bool = False, timeout: int | None = None) -> dict[str, Any]:
    effective_timeout = timeout or GIT_TIMEOUT_SECONDS
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    })
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = await create_owned_subprocess_exec(
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": "git executable not found"}

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        await release_owned_process(proc)
    except asyncio.TimeoutError:
        await finalize_process_tree(proc)
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"git command timed out after {effective_timeout}s",
            **tool_timeout_metadata("git"),
        }
    except asyncio.CancelledError:
        await finalize_process_tree(proc)
        raise
    return {
        "returncode": proc.returncode or 0,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


async def run_git(repo: GitRepo, args: list[str], *, read_only: bool = False, timeout: int | None = None) -> dict[str, Any]:
    result = await run_process(["git", *args], cwd=repo.repo_root, read_only=read_only, timeout=timeout)
    if result.get("timeout"):
        raise GitProcessTimeout(result)
    return result


async def discover_repo(ctx: ToolContext) -> GitRepo | None:
    proc = await run_process(["git", "rev-parse", "--show-toplevel"], cwd=ctx.workspace, read_only=True)
    if proc.get("timeout"):
        raise GitProcessTimeout(proc)
    if proc["returncode"] != 0:
        return None
    return GitRepo(
        repo_root=str(Path(proc["stdout"].strip()).resolve()),
        workspace=str(Path(ctx.workspace).resolve()),
    )
