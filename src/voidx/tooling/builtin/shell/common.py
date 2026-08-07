"""Platform-agnostic shell tool primitives — RouteHint, result factories, process termination.

Shared by bash (unix) and powershell (Windows) tools to avoid duplication.
"""

from __future__ import annotations

import asyncio

from voidx.platform.processes import (
    create_owned_process,
    create_owned_subprocess_exec,
    create_owned_subprocess_shell,
    finalize_process_tree,
    process_launch_options,
    release_owned_process,
)
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.shell_result import (
    RouteHint,
    build_blocked_result,
    build_hint_result,
    build_sandbox_result,
    build_success_result,
    build_timeout_result,
    resolve_shell_workspace,
)




async def maybe_route_hint(command: str, hint: RouteHint, ctx: ToolContext, source: str) -> ToolResult | None:
    """Execute a route hint when structured args and the target tool are available."""
    tool_invoker = ctx.tool_invoker
    if hint.tool_args is None or tool_invoker is None:
        return None
    if tool_invoker.get(hint.tool_id) is None:
        return None
    result = await tool_invoker.execute_tool(hint.tool_id, hint.tool_args, ctx)
    result.metadata = {
        **result.metadata,
        "tool": hint.tool_id,
        "routed_from": source.lower(),
        "routed_command": command,
        "routed_tool_args": hint.tool_args,
    }
    return result


# ── process termination ─────────────────────────────────────────────────────


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    await finalize_process_tree(proc)
