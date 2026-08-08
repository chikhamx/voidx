from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.slash import SlashHandler
from tests.test_slash.context import command_context
from voidx.config import Settings
from voidx.update.service import UpdateCheckResult, UpgradeResult


def _capture_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "voidx.agent.slash.handler.ui.print",
        lambda text="": output.append(str(text)),
    )
    monkeypatch.setattr(
        "voidx.agent.slash.handler.ui.error",
        lambda text="": output.append(f"ERROR: {text}"),
    )
    return output


@pytest.mark.asyncio
async def test_upgrade_check_dispatches_and_markssettings(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    settings = Settings(str(tmp_path))

    async def fake_check_for_update():
        return UpdateCheckResult(
            current_version="1.0.0",
            latest_version="9.0.0",
            update_available=True,
            message="voidx 9.0.0 is available.",
        )

    monkeypatch.setattr("voidx.update.service.check_for_update", fake_check_for_update)
    monkeypatch.setattr("voidx.update.service.upgrade_hint", lambda: "Run /upgrade now")

    handled = await SlashHandler(command_context(settings=settings)).dispatch("/upgrade check")

    assert handled is True
    assert settings.get_update_check_latest_version() == "9.0.0"
    assert any("voidx 9.0.0" in line for line in output)
    assert "[dim]Run /upgrade now[/dim]" in output


@pytest.mark.asyncio
async def test_upgrade_now_dispatches_perform_upgrade(monkeypatch):
    output = _capture_output(monkeypatch)

    async def fake_perform_upgrade():
        return UpgradeResult(ok=True, version="9.0.0", message="Upgraded voidx to 9.0.0.")

    monkeypatch.setattr("voidx.update.service.perform_upgrade", fake_perform_upgrade)

    handled = await SlashHandler(command_context()).dispatch("/upgrade now")

    assert handled is True
    assert output[0] == "[dim]Checking for updates...[/dim]"
    assert output[1] == "[green]Upgraded voidx to 9.0.0.[/green]"


@pytest.mark.asyncio
async def test_upgrade_now_uses_fresh_cached_latest_version(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    settings = Settings(str(tmp_path))
    settings.mark_update_check("9.0.0")
    calls: list[str | None] = []

    async def fake_perform_upgrade(version: str | None = None):
        calls.append(version)
        return UpgradeResult(ok=True, version=version, message="Upgraded voidx to 9.0.0.")

    monkeypatch.setattr("voidx.update.service.perform_upgrade", fake_perform_upgrade)

    handled = await SlashHandler(command_context(settings=settings)).dispatch("/upgrade now")

    assert handled is True
    assert calls == ["9.0.0"]
    assert output[0] == "[dim]Upgrading to voidx 9.0.0...[/dim]"


@pytest.mark.asyncio
async def test_upgrade_on_off_status_usesettings(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    settings = Settings(str(tmp_path))
    handler = SlashHandler(command_context(settings=settings))

    assert await handler.dispatch("/upgrade off") is True
    assert settings.get_update_check_enabled() is False

    assert await handler.dispatch("/upgrade on") is True
    assert settings.get_update_check_enabled() is True

    settings.mark_update_check("9.0.0", now=1000)
    assert await handler.dispatch("/upgrade status") is True

    assert any("enabled:" in line for line in output)
    assert any("latest seen:" in line and "9.0.0" in line for line in output)


def test_upgrade_commands_are_in_palette():
    from voidx.presentation.commands import COMMANDS

    assert ("/upgrade", "Check for voidx updates") in COMMANDS
    assert ("/upgrade now", "Upgrade voidx in the current Python environment") in COMMANDS
