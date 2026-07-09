from __future__ import annotations

import sys

import pytest

from voidx import selfupdate


def test_is_newer_compares_stable_versions() -> None:
    assert selfupdate.is_newer("2.2.2", "2.2.1") is True
    assert selfupdate.is_newer("2.10.0", "2.9.9") is True
    assert selfupdate.is_newer("2.2.1", "2.2.1") is False
    assert selfupdate.is_newer("2.2.1", "2.2.2") is False


def test_is_newer_ignores_prereleases() -> None:
    assert selfupdate.is_newer("2.3.0rc1", "2.2.1") is False
    assert selfupdate.is_newer("2.3.0.dev1", "2.2.1") is False
    assert selfupdate.is_newer("2.3.0+build.1", "2.2.1") is True


@pytest.mark.asyncio
async def test_check_for_update_reports_available(monkeypatch) -> None:
    async def fake_fetch_latest_version() -> str:
        return "9.0.0"

    monkeypatch.setattr(selfupdate, "fetch_latest_version", fake_fetch_latest_version)

    result = await selfupdate.check_for_update(current="1.0.0")

    assert result.latest_version == "9.0.0"
    assert result.update_available is True
    assert result.error is None


@pytest.mark.asyncio
async def test_check_for_update_reports_network_failure(monkeypatch) -> None:
    async def fake_fetch_latest_version() -> str:
        raise RuntimeError("offline")

    monkeypatch.setattr(selfupdate, "fetch_latest_version", fake_fetch_latest_version)

    result = await selfupdate.check_for_update(current="1.0.0")

    assert result.latest_version is None
    assert result.update_available is False
    assert result.error == "offline"


@pytest.mark.asyncio
async def test_perform_upgrade_refuses_npm_launcher(monkeypatch) -> None:
    monkeypatch.setenv("VOIDX_LAUNCHED_BY_NPM", "1")

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is False
    assert "npm update -g @chikhamx/voidx" in result.message


@pytest.mark.asyncio
async def test_perform_upgrade_refuses_non_virtualenv(monkeypatch) -> None:
    monkeypatch.delenv("VOIDX_LAUNCHED_BY_NPM", raising=False)
    monkeypatch.setattr(selfupdate, "_in_virtualenv", lambda: False)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is False
    assert "package manager" in result.message


@pytest.mark.asyncio
async def test_perform_upgrade_runs_pip_for_newer_stable_version(monkeypatch) -> None:
    monkeypatch.delenv("VOIDX_LAUNCHED_BY_NPM", raising=False)
    monkeypatch.setattr(selfupdate, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda pkg: "3.5.1")
    monkeypatch.setattr(selfupdate, "_can_import_voidx_cli", lambda: True)
    created: list[tuple[tuple[str, ...], dict]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command: str, **kwargs):
        created.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(selfupdate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is True
    assert len(created) == 2, "perform_upgrade must call pip install twice (voidx + voidx-cli)"

    # First call: voidx core
    command = created[0][0]
    assert command[:5] == (
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
    )
    assert "voidx==9.0.0" in command
    assert "voidx-cli" not in command
    assert created[0][1]["env"]["PIP_NO_INPUT"] == "1"

    # Second call: voidx-cli
    cli_command = created[1][0]
    assert cli_command[:5] == (
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
    )
    assert "voidx-cli==9.0.0" in cli_command
    assert created[1][1]["env"]["PIP_NO_INPUT"] == "1"


@pytest.mark.asyncio
async def test_perform_upgrade_rolls_back_when_voidx_cli_fails(monkeypatch) -> None:
    """voidx-cli install failure triggers automatic rollback to previous version."""
    monkeypatch.delenv("VOIDX_LAUNCHED_BY_NPM", raising=False)
    monkeypatch.setattr(selfupdate, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda pkg: "3.5.1")
    monkeypatch.setattr(selfupdate, "_can_import_voidx_cli", lambda: False)
    call_count = 0

    class FakeProcessOk:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command: str, **kwargs):
        nonlocal call_count
        call_count += 1
        return FakeProcessOk()

    monkeypatch.setattr(selfupdate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is False
    assert "rolled back" in result.message.lower() or "reverted" in result.message.lower()
    # Should have called pip install at least 3 times: voidx, voidx-cli, rollback voidx
    assert call_count >= 3


@pytest.mark.asyncio
async def test_perform_upgrade_succeeds_when_voidx_cli_importable(monkeypatch) -> None:
    """Upgrade succeeds when voidx-cli is importable after install."""
    monkeypatch.delenv("VOIDX_LAUNCHED_BY_NPM", raising=False)
    monkeypatch.setattr(selfupdate, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda pkg: "3.5.1")
    monkeypatch.setattr(selfupdate, "_can_import_voidx_cli", lambda: True)
    created: list[tuple[tuple[str, ...], dict]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command: str, **kwargs):
        created.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(selfupdate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is True
    assert len(created) == 2  # voidx + voidx-cli, no rollback


@pytest.mark.asyncio
async def test_perform_upgrade_updates_install_marker(monkeypatch, tmp_path) -> None:
    """Upgrade success updates .voidx-install-version marker so install.sh/voidx.js
    don't re-install on next launch."""
    monkeypatch.delenv("VOIDX_LAUNCHED_BY_NPM", raising=False)
    monkeypatch.setattr(selfupdate, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda pkg: "3.5.1")
    monkeypatch.setattr(selfupdate, "_can_import_voidx_cli", lambda: True)

    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n20260602\n3.12.13\n")
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command: str, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(selfupdate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is True
    marker_content = marker_path.read_text()
    assert marker_content.startswith("9.0.0\n")
    assert "20260602" in marker_content
    assert "3.12.13" in marker_content


@pytest.mark.asyncio
async def test_perform_upgrade_skips_marker_when_absent(monkeypatch, tmp_path) -> None:
    """Upgrade succeeds even when marker file doesn't exist (non-install.sh setup)."""
    monkeypatch.delenv("VOIDX_LAUNCHED_BY_NPM", raising=False)
    monkeypatch.setattr(selfupdate, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda pkg: "3.5.1")
    monkeypatch.setattr(selfupdate, "_can_import_voidx_cli", lambda: True)

    marker_path = tmp_path / ".voidx-install-version"
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command: str, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(selfupdate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is True
    assert not marker_path.exists()


@pytest.mark.asyncio
async def test_perform_upgrade_rollback_failure_reports_manual_fix(monkeypatch) -> None:
    """If rollback itself fails, message tells user to fix manually."""
    monkeypatch.delenv("VOIDX_LAUNCHED_BY_NPM", raising=False)
    monkeypatch.setattr(selfupdate, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda pkg: "3.5.1")
    monkeypatch.setattr(selfupdate, "_can_import_voidx_cli", lambda: False)
    call_count = 0

    class FakeProcessOk:
        returncode = 0

        async def communicate(self):
            return b"", b""

    class FakeProcessFail:
        returncode = 1

        async def communicate(self):
            return b"", b"rollback failed"

    async def fake_create_subprocess_exec(*command: str, **kwargs):
        nonlocal call_count
        call_count += 1
        # voidx install ok, voidx-cli install ok, rollback fails
        return FakeProcessOk() if call_count <= 2 else FakeProcessFail()

    monkeypatch.setattr(selfupdate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is False
    assert "manual" in result.message.lower() or "pip install" in result.message.lower()
    assert "voidx" in result.message.lower()


def test_can_import_voidx_cli_true_when_importable(monkeypatch) -> None:
    """_can_import_voidx_cli returns True when voidx_cli is actually importable."""
    from importlib.metadata import PackageNotFoundError

    def fake_version(name):
        if name == "voidx-cli":
            return "3.6.0"
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", fake_version)

    import importlib
    original_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "voidx_cli":
            return original_import("voidx_cli")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("importlib.import_module", fake_import)

    assert selfupdate._can_import_voidx_cli() is True


def test_can_import_voidx_cli_false_when_metadata_exists_but_import_fails(monkeypatch) -> None:
    """_can_import_voidx_cli returns False when .dist-info exists but module is not importable.

    This happens when pip install is interrupted — the .dist-info directory is
    written before all .py files are extracted, leaving metadata without code.
    """
    from importlib.metadata import PackageNotFoundError

    def fake_version(name):
        if name == "voidx-cli":
            return "3.6.0"
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", fake_version)

    def fake_import(name, *args, **kwargs):
        if name == "voidx_cli":
            raise ModuleNotFoundError(f"No module named '{name}'")
        return __import__(name, *args, **kwargs)

    monkeypatch.setattr("importlib.import_module", fake_import)

    assert selfupdate._can_import_voidx_cli() is False


def test_can_import_voidx_cli_false_when_not_installed(monkeypatch) -> None:
    """_can_import_voidx_cli returns False when voidx-cli is not installed at all."""
    from importlib.metadata import PackageNotFoundError

    def fake_version(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", fake_version)

