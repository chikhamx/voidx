from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

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


def _verification(
    ok: bool,
    *,
    version: str | None = None,
    core_version: str | None = None,
    cli_version: str | None = None,
    message: str = "verification failed",
) -> SimpleNamespace:
    resolved_core = core_version if core_version is not None else version
    resolved_cli = cli_version if cli_version is not None else version
    return SimpleNamespace(
        ok=ok,
        core_version=resolved_core,
        cli_version=resolved_cli,
        message=message if not ok else "ok",
    )


def _setup_upgrade(monkeypatch) -> None:
    monkeypatch.delenv("VOIDX_LAUNCHED_BY_NPM", raising=False)
    monkeypatch.setattr(selfupdate, "_in_virtualenv", lambda: True)


@pytest.mark.asyncio
async def test_perform_upgrade_installs_exact_pair_in_one_pip_command(monkeypatch) -> None:
    _setup_upgrade(monkeypatch)
    verification_results = iter([
        _verification(True, version="3.5.1"),
        _verification(True, version="9.0.0"),
    ])
    verify_calls: list[str | None] = []
    pip_calls: list[tuple[tuple[str, ...], bool]] = []

    async def fake_verify(expected_version, env, timeout):
        verify_calls.append(expected_version)
        return next(verification_results)

    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        pip_calls.append((specs, force_reinstall))
        return selfupdate._PipResult(ok=True, message="ok")

    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)
    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is True
    assert verify_calls == [None, "9.0.0"]
    assert pip_calls == [
        (("voidx==9.0.0", "voidx-cli==9.0.0"), False),
    ]


@pytest.mark.asyncio
async def test_perform_upgrade_force_repairs_pair_once_before_rollback(monkeypatch) -> None:
    _setup_upgrade(monkeypatch)
    verification_results = iter([
        _verification(True, version="3.5.1"),
        _verification(False, core_version="9.0.0", cli_version="3.5.1"),
        _verification(False, core_version="9.0.0", cli_version="3.5.1"),
        _verification(True, version="3.5.1"),
    ])
    pip_calls: list[tuple[tuple[str, ...], bool]] = []

    async def fake_verify(expected_version, env, timeout):
        return next(verification_results)

    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        pip_calls.append((specs, force_reinstall))
        return selfupdate._PipResult(ok=True, message="ok")

    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)
    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is False
    assert "rolled back" in result.message.lower()
    assert pip_calls == [
        (("voidx==9.0.0", "voidx-cli==9.0.0"), False),
        (("voidx==9.0.0", "voidx-cli==9.0.0"), True),
        (("voidx==3.5.1", "voidx-cli==3.5.1"), False),
    ]


@pytest.mark.asyncio
async def test_perform_upgrade_does_not_claim_rollback_from_invalid_pre_state(
    monkeypatch,
    tmp_path,
) -> None:
    _setup_upgrade(monkeypatch)
    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n20260602\n3.12.13\n")
    verification_results = iter([
        _verification(False, core_version="3.5.1", cli_version=None),
        _verification(False, core_version="9.0.0", cli_version="3.5.1"),
        _verification(False, core_version="9.0.0", cli_version="3.5.1"),
    ])
    pip_calls: list[tuple[tuple[str, ...], bool]] = []

    async def fake_verify(expected_version, env, timeout):
        return next(verification_results)

    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        pip_calls.append((specs, force_reinstall))
        return selfupdate._PipResult(ok=True, message="ok")

    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)
    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is False
    assert "rolled back" not in result.message.lower()
    assert "voidx==9.0.0" in result.message
    assert "voidx-cli==9.0.0" in result.message
    assert pip_calls == [
        (("voidx==9.0.0", "voidx-cli==9.0.0"), False),
        (("voidx==9.0.0", "voidx-cli==9.0.0"), True),
    ]
    assert not marker_path.exists()


@pytest.mark.asyncio
async def test_perform_upgrade_updates_install_marker(monkeypatch, tmp_path) -> None:
    _setup_upgrade(monkeypatch)
    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n20260602\n3.12.13\n")
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)
    verification_results = iter([
        _verification(True, version="3.5.1"),
        _verification(True, version="9.0.0"),
    ])

    async def fake_verify(expected_version, env, timeout):
        return next(verification_results)

    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        return selfupdate._PipResult(ok=True, message="ok")

    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)
    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is True
    marker_content = marker_path.read_text()
    assert marker_content.startswith("9.0.0\n")
    assert "20260602" in marker_content
    assert "3.12.13" in marker_content


@pytest.mark.asyncio
async def test_perform_upgrade_skips_marker_when_absent(monkeypatch, tmp_path) -> None:
    _setup_upgrade(monkeypatch)
    marker_path = tmp_path / ".voidx-install-version"
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)
    verification_results = iter([
        _verification(True, version="3.5.1"),
        _verification(True, version="9.0.0"),
    ])

    async def fake_verify(expected_version, env, timeout):
        return next(verification_results)

    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        return selfupdate._PipResult(ok=True, message="ok")

    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)
    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is True
    assert not marker_path.exists()


@pytest.mark.asyncio
async def test_perform_upgrade_rollback_failure_reports_manual_fix(monkeypatch) -> None:
    _setup_upgrade(monkeypatch)
    verification_results = iter([
        _verification(True, version="3.5.1"),
        _verification(False, core_version="9.0.0", cli_version="3.5.1"),
        _verification(False, core_version="9.0.0", cli_version="3.5.1"),
    ])
    pip_results = iter([
        selfupdate._PipResult(ok=True, message="ok"),
        selfupdate._PipResult(ok=True, message="ok"),
        selfupdate._PipResult(ok=False, message="rollback failed"),
    ])

    async def fake_verify(expected_version, env, timeout):
        return next(verification_results)

    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        return next(pip_results)

    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)
    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)

    result = await selfupdate.perform_upgrade("9.0.0")

    assert result.ok is False
    assert "manual" in result.message.lower() or "pip install" in result.message.lower()
    assert "voidx" in result.message.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"core_version": None, "cli_version": "9.0.0"},
        {"core_version": "9.0.0", "cli_version": None},
        {"core_version": "9.0.0", "cli_version": "3.5.1"},
        {"core_version": "3.5.1", "cli_version": "9.0.0"},
        {"core_version": "9.0.0", "cli_version": "9.0.0", "core_import": False},
        {"core_version": "9.0.0", "cli_version": "9.0.0", "cli_import": False},
        {"core_version": "9.0.0", "cli_version": "9.0.0", "entrypoint_ok": False},
        {
            "core_version": "9.0.0",
            "cli_version": "9.0.0",
            "entrypoint_version": "3.5.1",
        },
    ],
)
async def test_verify_installation_rejects_incomplete_or_mismatched_pair(monkeypatch, payload) -> None:
    complete_payload = {
        "core_version": "9.0.0",
        "cli_version": "9.0.0",
        "core_import": True,
        "cli_import": True,
        "entrypoint_ok": True,
        "entrypoint_version": "9.0.0",
        **payload,
    }

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return json.dumps(complete_payload).encode(), b""

    async def fake_create_subprocess_exec(*command: str, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(selfupdate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await selfupdate._verify_installation("9.0.0", {}, 1.0)

    assert result.ok is False


def test_verification_probe_excludes_current_directory_from_imports() -> None:
    assert "os.getcwd()" in selfupdate._VERIFICATION_PROBE
    assert "sys.path =" in selfupdate._VERIFICATION_PROBE


@pytest.mark.asyncio
async def test_verify_installation_accepts_coherent_existing_pair(monkeypatch) -> None:
    payload = {
        "core_version": "3.5.1",
        "cli_version": "3.5.1",
        "core_import": True,
        "cli_import": True,
        "entrypoint_ok": True,
        "entrypoint_version": "3.5.1",
    }

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return json.dumps(payload).encode(), b""

    async def fake_create_subprocess_exec(*command: str, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(selfupdate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await selfupdate._verify_installation(None, {}, 1.0)

    assert result.ok is True
    assert result.core_version == "3.5.1"
    assert result.cli_version == "3.5.1"


def test_update_install_marker_replaces_file_atomically(monkeypatch, tmp_path) -> None:
    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n20260602\n3.12.13\n")
    replace_calls: list[tuple[object, object]] = []
    original_replace = os.replace

    def recording_replace(source, destination):
        replace_calls.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)
    monkeypatch.setattr(selfupdate.os, "replace", recording_replace)

    selfupdate._update_install_marker("9.0.0")

    assert len(replace_calls) == 1
    assert replace_calls[0][1] == marker_path
    assert marker_path.read_text().startswith("9.0.0\n")


def test_windows_locked_file_failure_includes_installer_guidance(monkeypatch) -> None:
    monkeypatch.setattr(selfupdate.sys, "platform", "win32")

    message = selfupdate._format_upgrade_failure(
        "Upgrade failed: [WinError 32] The process cannot access the file",
        "9.0.0",
    )

    assert "exit voidx" in message.lower()
    assert "install.ps1" in message


def test_update_install_marker_preserves_4_line_windows_format(monkeypatch, tmp_path) -> None:
    """Windows markers include a 4th platform-target line; _update_install_marker
    must preserve all lines and only replace the version line."""
    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n20260602\n3.12.13\nx86_64-pc-windows-msvc\n")
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)

    selfupdate._update_install_marker("9.0.0")

    lines = marker_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert lines[0] == "9.0.0"
    assert lines[1] == "20260602"
    assert lines[2] == "3.12.13"
    assert lines[3] == "x86_64-pc-windows-msvc"


def test_update_install_marker_skips_malformed_marker_with_fewer_than_3_lines(
    monkeypatch, tmp_path
) -> None:
    """A marker with < 3 lines is malformed; _update_install_marker must leave it
    untouched rather than writing a partial file."""
    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n")
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)

    selfupdate._update_install_marker("9.0.0")

    assert marker_path.read_text() == "3.5.1\n"


def test_clear_install_marker_removes_file(monkeypatch, tmp_path) -> None:
    """_clear_install_marker must delete the marker file when it exists."""
    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n20260602\n3.12.13\n")
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)

    selfupdate._clear_install_marker()

    assert not marker_path.exists()


def test_clear_install_marker_is_noop_when_marker_absent(monkeypatch, tmp_path) -> None:
    """_clear_install_marker must not raise when the marker file is already absent."""
    marker_path = tmp_path / ".voidx-install-version"
    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)

    selfupdate._clear_install_marker()

    assert not marker_path.exists()


def test_format_upgrade_failure_on_non_windows_does_not_add_installer_guidance(
    monkeypatch,
) -> None:
    """On non-Windows, _format_upgrade_failure must return the original message
    without appending Windows-specific installer guidance."""
    monkeypatch.setattr(selfupdate.sys, "platform", "linux")

    original = "Upgrade failed: some pip error"
    message = selfupdate._format_upgrade_failure(original, "9.0.0")

    assert message == original


@pytest.mark.asyncio
async def test_rollback_to_verifies_restored_pair_independently(monkeypatch) -> None:
    """_rollback_to must verify the restored pair after pip install, not just
    trust that pip succeeded."""
    verify_calls: list[str | None] = []
    pip_calls: list[tuple[str, ...]] = []

    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        pip_calls.append(specs)
        return selfupdate._PipResult(ok=True, message="ok")

    async def fake_verify(expected_version, env, timeout):
        verify_calls.append(expected_version)
        return selfupdate._VerificationResult(
            ok=True,
            core_version=expected_version,
            cli_version=expected_version,
            message="ok",
        )

    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)
    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)

    result = await selfupdate._rollback_to("3.5.1", {}, 1.0)

    assert result is None
    assert pip_calls == [("voidx==3.5.1", "voidx-cli==3.5.1")]
    assert verify_calls == ["3.5.1"]


@pytest.mark.asyncio
async def test_rollback_to_returns_error_when_verification_fails(monkeypatch) -> None:
    """_rollback_to must return the verification failure message when the restored
    pair does not verify, rather than claiming success."""
    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        return selfupdate._PipResult(ok=True, message="ok")

    async def fake_verify(expected_version, env, timeout):
        return selfupdate._VerificationResult(
            ok=False,
            core_version="3.5.1",
            cli_version=None,
            message="voidx-cli is not installed",
        )

    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)
    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)

    result = await selfupdate._rollback_to("3.5.1", {}, 1.0)

    assert result is not None
    assert "voidx-cli is not installed" in result


@pytest.mark.asyncio
async def test_rollback_to_returns_error_when_pip_fails(monkeypatch) -> None:
    """_rollback_to must return the pip failure message without attempting
    verification when the rollback pip install itself fails."""
    verify_calls: list[str | None] = []

    async def fake_pip_install(specs, env, timeout, *, force_reinstall=False):
        return selfupdate._PipResult(ok=False, message="pip network error")

    async def fake_verify(expected_version, env, timeout):
        verify_calls.append(expected_version)
        return selfupdate._VerificationResult(
            ok=True, core_version="3.5.1", cli_version="3.5.1", message="ok"
        )

    monkeypatch.setattr(selfupdate, "_pip_install", fake_pip_install)
    monkeypatch.setattr(selfupdate, "_verify_installation", fake_verify, raising=False)

    result = await selfupdate._rollback_to("3.5.1", {}, 1.0)

    assert result is not None
    assert "pip network error" in result
    assert verify_calls == []


def test_update_install_marker_logs_failure(monkeypatch, tmp_path) -> None:
    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n20260602\n3.12.13\n")
    errors = []

    def failing_replace(source, destination):
        raise OSError("replace failed")

    def fake_log_internal_error(exc, *, context, **kwargs):
        errors.append((context, str(exc)))

    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)
    monkeypatch.setattr(selfupdate.os, "replace", failing_replace)
    monkeypatch.setattr(selfupdate, "log_internal_error", fake_log_internal_error)

    selfupdate._update_install_marker("9.0.0")

    assert errors == [("selfupdate_marker_write", "replace failed")]


def test_clear_install_marker_logs_failure(monkeypatch, tmp_path) -> None:
    marker_path = tmp_path / ".voidx-install-version"
    marker_path.write_text("3.5.1\n20260602\n3.12.13\n")
    errors = []

    def failing_unlink(*args, **kwargs):
        raise OSError("unlink failed")

    def fake_log_internal_error(exc, *, context, **kwargs):
        errors.append((context, str(exc)))

    monkeypatch.setattr(selfupdate, "_install_marker_path", lambda: marker_path)
    monkeypatch.setattr(type(marker_path), "unlink", failing_unlink)
    monkeypatch.setattr(selfupdate, "log_internal_error", fake_log_internal_error)

    selfupdate._clear_install_marker()

    assert errors == [("selfupdate_marker_clear", "unlink failed")]
