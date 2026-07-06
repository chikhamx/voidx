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


def test_can_import_voidx_cli_uses_metadata_not_find_spec(monkeypatch) -> None:
    """Regression: _can_import_voidx_cli must use importlib.metadata, not find_spec.

    pip install runs in a subprocess, so the current process's
    sys.path_importer_cache won't reflect newly installed packages.
    importlib.metadata re-reads .dist-info from site-packages on every call.
    """
    import importlib

    # find_spec must NOT be called (it uses stale import cache)
    find_spec_calls = []
    original_find_spec = importlib.util.find_spec if hasattr(importlib, "util") else None
    if original_find_spec is not None:
        monkeypatch.setattr(
            "importlib.util.find_spec",
            lambda name: find_spec_calls.append(name) or None,
        )

    # importlib.metadata.version should be the source of truth
    from importlib.metadata import PackageNotFoundError

    def fake_version(name):
        if name == "voidx-cli":
            return "3.5.2"
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", fake_version)

    assert selfupdate._can_import_voidx_cli() is True
    assert find_spec_calls == []  # find_spec must not be used
