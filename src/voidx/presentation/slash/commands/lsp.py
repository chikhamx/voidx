"""Slash /lsp commands."""
from __future__ import annotations

from voidx.lsp.config import lsp_config_path


class LspCommandsMixin:
    async def _lsp(self, args: str) -> None:
        parts = args.split(None, 1)
        action = parts[0] if parts else "status"
        target = parts[1].strip() if len(parts) > 1 else ""

        if action in ("", "status"):
            self._lsp_status()
        elif action == "doctor":
            self._lsp_doctor()
        elif action == "restart":
            await self._lsp_restart(target or None)
        elif action == "servers":
            self._lsp_servers()
        else:
            self.integrations_port.ui.error("Usage: /lsp [status|doctor|restart|servers]")

    def _lsp_status(self) -> None:
        manager = self.integrations_port.lsp_ops
        if manager is None:
            self.integrations_port.ui.error("No LSP manager available.")
            return
        self.integrations_port.ui.print("[bold]LSP status:[/bold]")
        for status in manager.statuses():
            label = {
                "initializing": "[dim]initializing[/dim]",
                "connected": "[green]connected[/green]",
                "disconnected": "[dim]disconnected[/dim]",
                "disabled": "[dim]disabled[/dim]",
                "error": "[red]error[/red]",
            }.get(status.status, status.status)
            detail = f" · pid {status.pid}" if status.pid else ""
            docs = f" · {status.open_documents} doc{'s' if status.open_documents != 1 else ''}"
            self.integrations_port.ui.print(f"  [cyan]{status.language}[/cyan] · {label}{detail}{docs}")
            if status.error_message:
                self.integrations_port.ui.print(f"    [red]{status.error_message}[/red]")
        self.integrations_port.ui.print("[dim]Usage: /lsp status|doctor|restart|servers[/dim]")

    def _lsp_doctor(self) -> None:
        manager = self.integrations_port.lsp_ops
        if manager is None:
            self.integrations_port.ui.error("No LSP manager available.")
            return
        if getattr(manager, "initializing", False) or not getattr(manager, "initialized", True):
            self.integrations_port.ui.print("[dim]LSP servers are still initializing.[/dim]")
            return
        self.integrations_port.ui.print("[bold]LSP doctor:[/bold]")
        missing = 0
        disabled = 0
        auto_detected = 0
        for check in manager.doctor():
            if not check.enabled:
                disabled += 1
                self.integrations_port.ui.print(f"  [cyan]{check.language}[/cyan] · [dim]disabled[/dim] · {check.command}")
                continue
            source = f" [dim]({check.detected_source})[/dim]" if check.detected_source else ""
            if check.available:
                if check.detected_source:
                    auto_detected += 1
                self.integrations_port.ui.print(
                    f"  [cyan]{check.language}[/cyan] · [green]ok[/green] · "
                    f"{check.command} [dim]({check.resolved_path})[/dim]{source}"
                )
                continue
            missing += 1
            self.integrations_port.ui.print(f"  [cyan]{check.language}[/cyan] · [red]missing[/red] · {check.command}")
            if check.install_hint:
                self.integrations_port.ui.print(f"    [dim]{check.install_hint}[/dim]")
        if missing:
            self.integrations_port.ui.print(f"[yellow]{missing} LSP server{'s' if missing != 1 else ''} missing.[/yellow]")
        elif disabled:
            self.integrations_port.ui.print("[dim]No missing enabled LSP servers.[/dim]")
        else:
            msg = "All enabled LSP servers are available."
            if auto_detected:
                msg += f" ({auto_detected} auto-detected)"
            self.integrations_port.ui.print(f"[green]{msg}[/green]")

    async def _lsp_restart(self, language: str | None) -> None:
        manager = self.integrations_port.lsp_ops
        if manager is None:
            self.integrations_port.ui.error("No LSP manager available.")
            return
        await manager.restart(language)
        target = language or "all servers"
        self.integrations_port.ui.print(f"[green]✓ restarted {target}[/green]")

    def _lsp_servers(self) -> None:
        manager = self.integrations_port.lsp_ops
        workspace = self.integrations_port.workspace
        self.integrations_port.ui.print("[bold]LSP servers:[/bold]")
        self.integrations_port.ui.print(f"[dim]{lsp_config_path(workspace)}[/dim]")
        if manager is None:
            self.integrations_port.ui.error("No LSP manager available.")
            return
        if getattr(manager, "initializing", False) or not getattr(manager, "initialized", True):
            self.integrations_port.ui.print("[dim]LSP servers are still initializing.[/dim]")
            return
        for config in manager.servers.values():
            state = "[green]enabled[/green]" if config.enabled else "[dim]disabled[/dim]"
            exts = ", ".join(config.extensions) or "no extensions"
            command = " ".join([config.command, *config.args]).strip()
            self.integrations_port.ui.print(f"  [cyan]{config.language}[/cyan] · {state} · [dim]{exts}[/dim]")
            self.integrations_port.ui.print(f"    {command}")

