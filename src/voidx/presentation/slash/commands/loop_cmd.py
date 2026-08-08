"""Slash /loop commands."""
from __future__ import annotations

from voidx.agent.domain.automation.loop import LoopSpec
from voidx.presentation.slash.helpers import _parse_interval


class LoopCmdCommandsMixin:
    async def _loop(self, args: str) -> None:
        arg = args.strip()
        if not arg:
            await self._switch_profile("loop")
            return
        if arg == "help":
            self.automation_port.ui.print("[dim]Usage: /loop [interval] <prompt>, /loop stop, /loop status, /loop resume[/dim]")
            return
        service = self.automation_port.loop_service
        if service is None:
            self.automation_port.ui.error("/loop is not available in this session.")
            return
        session = self.automation_port.session
        parent_thread_id = getattr(session, "id", None)
        if arg == "stop":
            stopped = await service.stop(parent_thread_id)
            self.automation_port.ui.print("[dim]/loop stopped.[/dim]" if stopped else "[dim]No active /loop.[/dim]")
            return
        if arg == "resume":
            status = await service.resume(parent_thread_id)
            if status is None:
                self.automation_port.ui.print("[dim]No previous /loop to resume.[/dim]")
            else:
                self.automation_port.ui.print(f"[dim]/loop resumed · {status.loop_thread_id}.[/dim]")
            return
        if arg == "status":
            status = await service.status(parent_thread_id)
            if status is None:
                self.automation_port.ui.print("[dim]No active /loop.[/dim]")
            else:
                self.automation_port.ui.print(f"[dim]/loop active: {status}[/dim]")
            return
        interval_seconds, prompt = _parse_interval(arg)
        if not prompt.strip():
            self.automation_port.ui.error("/loop requires a prompt.")
            return
        try:
            status = await service.start(
                parent_thread_id,
                LoopSpec(prompt=prompt.strip(), interval_seconds=interval_seconds),
            )
        except (ValueError, RuntimeError) as exc:
            self.automation_port.ui.error(str(exc))
            return
        mode = "dynamic" if interval_seconds is None else f"every {int(interval_seconds)}s"
        self.automation_port.ui.print(f"[dim]/loop started ({mode}) · {status.loop_thread_id}.[/dim]")

