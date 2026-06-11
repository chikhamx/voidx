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
    assert created
    command = created[0][0]
    assert command[:5] == (
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
    )
    assert "voidx==9.0.0" in command
    assert created[0][1]["env"]["PIP_NO_INPUT"] == "1"
