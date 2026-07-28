"""Slash /loop commands."""
from __future__ import annotations

from voidx.agent.domain.loop import LoopSpec
from voidx.runtime.ui import ui
from voidx.agent.slash.helpers import _parse_interval


class LoopCmdCommandsMixin:
    async def _loop(self, args: str) -> None:
        service = getattr(self.host, "loop_service", None)
        if service is None:
            ui.error("/loop is not available in this session.")
            return

        arg = args.strip()
        if not arg or arg == "help":
            ui.print("[dim]Usage: /loop [interval] <prompt>, /loop stop, /loop status, /loop resume[/dim]")
            return
        session = getattr(self.host, "session", None)
        parent_thread_id = getattr(session, "id", None)
        if arg == "stop":
            stopped = await service.stop(parent_thread_id)
            ui.print("[dim]/loop stopped.[/dim]" if stopped else "[dim]No active /loop.[/dim]")
            return
        if arg == "resume":
            status = await service.resume(parent_thread_id)
            if status is None:
                ui.print("[dim]No previous /loop to resume.[/dim]")
            else:
                ui.print(f"[dim]/loop resumed · {status.loop_thread_id}.[/dim]")
            return
        if arg == "status":
            status = await service.status(parent_thread_id)
            if status is None:
                ui.print("[dim]No active /loop.[/dim]")
            else:
                ui.print(f"[dim]/loop active: {status}[/dim]")
            return
        interval_seconds, prompt = _parse_interval(arg)
        if not prompt.strip():
            ui.error("/loop requires a prompt.")
            return
        try:
            status = await service.start(
                parent_thread_id,
                LoopSpec(prompt=prompt.strip(), interval_seconds=interval_seconds),
            )
        except (ValueError, RuntimeError) as exc:
            ui.error(str(exc))
            return
        mode = "dynamic" if interval_seconds is None else f"every {int(interval_seconds)}s"
        ui.print(f"[dim]/loop started ({mode}) · {status.loop_thread_id}.[/dim]")

