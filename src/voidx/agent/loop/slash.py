"""Slash command support for /loop."""

from __future__ import annotations

import re

from voidx.agent.loop.prompt_source import PromptSource
from voidx.tools.service import BashTool, ToolContext
from voidx.runtime.ui import ui

_INTERVAL_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[smhd])$")
_TRAILING_EVERY_RE = re.compile(r"\s+every\s+(?P<value>\d+)(?P<unit>[smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class SlashLoopMixin:
    async def _loop(self, args: str) -> None:
        manager = getattr(self.host, "loop_manager", None)
        if manager is None:
            ui.error("/loop is not available in this session.")
            return

        arg = args.strip()
        if not arg or arg == "help":
            ui.print("[dim]Usage: /loop [interval] <prompt>, /loop stop, /loop status[/dim]")
            return
        if arg == "stop":
            manager.stop()
            ui.print("[dim]/loop stopped.[/dim]")
            return
        if arg == "status":
            status = manager.status()
            if status is None:
                ui.print("[dim]No active /loop.[/dim]")
            else:
                ui.print(f"[dim]/loop active: {status}[/dim]")
            return

        interval_seconds, prompt = _parse_interval(arg)
        if not prompt.strip():
            ui.error("/loop requires a prompt.")
            return
        session = getattr(self.host, "session", None)
        ctx = _tool_context_for_host(self.host)
        manager.start(
            PromptSource.from_raw(prompt.strip()),
            interval_seconds,
            bash_tool=BashTool(),
            ctx=ctx,
            session_id=getattr(session, "id", None),
        )
        mode = "dynamic" if interval_seconds is None else f"every {int(interval_seconds)}s"
        ui.print(f"[dim]/loop started ({mode}).[/dim]")


def _parse_interval(args: str) -> tuple[float | None, str]:
    parts = args.split(None, 1)
    if parts:
        match = _INTERVAL_RE.match(parts[0])
        if match:
            prompt = parts[1] if len(parts) > 1 else ""
            return _interval_seconds(match), prompt
    match = _TRAILING_EVERY_RE.search(args)
    if match:
        prompt = args[:match.start()].strip()
        return _interval_seconds(match), prompt
    return None, args


def _interval_seconds(match: re.Match[str]) -> float:
    seconds = int(match.group("value")) * _UNIT_SECONDS[match.group("unit")]
    return float(max(seconds, 60))


def _tool_context_for_host(host) -> ToolContext:
    permission = getattr(host, "permission", None)
    workspace = getattr(host, "workspace", ".")
    session = getattr(host, "session", None)
    kwargs = {
        "workspace": workspace,
        "session_id": getattr(session, "id", "default") or "default",
        "loop_manager": getattr(host, "loop_manager", None),
        "tool_registry": getattr(host, "tools", None),
    }
    if permission is not None:
        kwargs.update(
            permission_mode=getattr(permission, "permission_mode", "safe"),
            sandbox_readable_files=list(getattr(permission, "sandbox_readable_files", [])),
            sandbox_readable_dirs=list(getattr(permission, "sandbox_readable_dirs", [])),
            sandbox_writable_files=list(getattr(permission, "sandbox_writable_files", [])),
            sandbox_writable_dirs=list(getattr(permission, "sandbox_writable_dirs", [])),
            get_access_grants=getattr(permission, "get_access_grants", None),
            get_revocation_epoch=lambda: getattr(permission, "revocation_epoch", 0),
            add_grant=getattr(permission, "add_grant", None),
            acquire_grant_targets=getattr(permission, "acquire_grant_targets", None),
            acquire_execution_lease=getattr(permission, "execution_lease_for_tool", None),
            process_sandbox=getattr(permission, "process_sandbox", None),
        )
    return ToolContext(**kwargs)
