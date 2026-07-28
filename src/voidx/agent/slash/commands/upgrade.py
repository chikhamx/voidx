"""Slash /upgrade commands."""
from __future__ import annotations

from voidx.runtime.ui import ui
from voidx.selfupdate import check_for_update, is_newer, perform_upgrade, upgrade_hint
from voidx.agent.slash.helpers import _format_timestamp, _format_upgrade_success


class UpgradeCommandsMixin:
    async def _upgrade(self, args: str) -> None:
        action = args.strip().lower() or "check"
        if action == "check":
            await self._upgrade_check()
        elif action == "now":
            await self._upgrade_now()
        elif action == "on":
            self._upgrade_set_enabled(True)
        elif action == "off":
            self._upgrade_set_enabled(False)
        elif action == "status":
            self._upgrade_status()
        else:
            ui.error("Usage: /upgrade [check|now|on|off|status]")

    async def _upgrade_check(self) -> None:
        result = await check_for_update()
        settings = self.host.settings
        mark_update_check = getattr(settings, "mark_update_check", None)
        if callable(mark_update_check):
            mark_update_check(result.latest_version)
        if result.error:
            ui.error(result.message)
            return
        ui.print(result.message)
        if result.update_available:
            ui.print(f"[dim]{upgrade_hint()}[/dim]")

    async def _upgrade_now(self) -> None:
        target = self._cached_upgrade_target()
        if target is not None:
            ui.print(f"[dim]Upgrading to voidx {target}...[/dim]")
            result = await perform_upgrade(target)
        else:
            ui.print("[dim]Checking for updates...[/dim]")
            result = await perform_upgrade()
        if result.ok:
            ui.print(_format_upgrade_success(result))
        else:
            ui.error(result.message)

    def _upgrade_set_enabled(self, enabled: bool) -> None:
        settings = self.host.settings
        if settings is None:
            ui.error("No settings file available.")
            return
        path = settings.set_update_check_enabled(enabled)
        state = "enabled" if enabled else "disabled"
        ui.print(f"[dim]Startup update checks {state}. Saved to {path}[/dim]")

    def _upgrade_status(self) -> None:
        settings = self.host.settings
        if settings is None:
            ui.error("No settings file available.")
            return
        enabled = "on" if settings.get_update_check_enabled() else "off"
        checked_at = _format_timestamp(settings.get_update_check_last_checked_at())
        latest = settings.get_update_check_latest_version() or "unknown"
        ui.print("[bold]Upgrade checks:[/bold]")
        ui.print(f"  enabled: [cyan]{enabled}[/cyan]")
        ui.print(f"  last checked: [cyan]{checked_at}[/cyan]")
        ui.print(f"  latest seen: [cyan]{latest}[/cyan]")

    def _cached_upgrade_target(self) -> str | None:
        settings = self.host.settings
        if settings is None:
            return None
        update_check_due = getattr(settings, "update_check_due", None)
        if not callable(update_check_due) or update_check_due():
            return None
        get_latest = getattr(settings, "get_update_check_latest_version", None)
        latest = get_latest() if callable(get_latest) else None
        if isinstance(latest, str) and is_newer(latest):
            return latest
        return None

