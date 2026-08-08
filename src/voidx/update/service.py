"""Version check and explicit self-update helpers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from importlib.metadata import PackageNotFoundError, version as package_version
from voidx.observability import log_internal_error
from voidx.observability.tool_log import log_tool_event


PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
_PRE_RELEASE_RE = re.compile(r"(?:a|b|rc|dev)\d*", re.IGNORECASE)
_RELEASE_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str | None
    update_available: bool
    message: str
    error: str | None = None


@dataclass(frozen=True)
class UpgradeResult:
    ok: bool
    version: str | None
    message: str


@dataclass(frozen=True)
class _VerificationResult:
    ok: bool
    core_version: str | None
    cli_version: str | None
    message: str


_VERIFICATION_PROBE = r"""
import importlib
import json
import os
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version

current_directory = os.path.normcase(os.path.realpath(os.getcwd()))
sys.path = [
    entry
    for entry in sys.path
    if entry and os.path.normcase(os.path.realpath(entry)) != current_directory
]

def installed_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None
    except Exception:
        return None

payload = {
    "core_version": installed_version("voidx"),
    "cli_version": installed_version("voidx-cli"),
    "core_import": False,
    "cli_import": False,
    "entrypoint_ok": False,
    "entrypoint_version": None,
}

try:
    importlib.import_module("voidx")
    payload["core_import"] = True
except Exception as exc:
    payload["core_error"] = str(exc)

try:
    importlib.import_module("voidx_cli")
    payload["cli_import"] = True
except Exception as exc:
    payload["cli_error"] = str(exc)

executable = os.path.join(
    os.path.dirname(sys.executable),
    "voidx.exe" if os.name == "nt" else "voidx",
)
try:
    completed = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    payload["entrypoint_ok"] = completed.returncode == 0
    match = re.search(r"\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?", output)
    payload["entrypoint_version"] = match.group(0) if match else None
    if completed.returncode != 0:
        payload["entrypoint_error"] = output
except Exception as exc:
    payload["entrypoint_error"] = str(exc)

print(json.dumps(payload))
"""




def current_version() -> str:
    try:
        return package_version("voidx")
    except PackageNotFoundError:
        return "0.0.0"

async def fetch_latest_version(package: str = "voidx", timeout: float = 5.0) -> str | None:
    """Fetch the latest package version from PyPI JSON metadata."""
    url = PYPI_JSON_URL.format(package=quote(package))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    info = data.get("info") if isinstance(data, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    return version if isinstance(version, str) and version.strip() else None


def is_newer(latest: str, current: str | None = None) -> bool:
    """Return True when latest is a newer stable release than current."""
    current = current or current_version()
    latest_key = _stable_release_key(latest)
    current_key = _stable_release_key(current)
    if latest_key is None or current_key is None:
        return False
    width = max(len(latest_key), len(current_key))
    latest_padded = latest_key + (0,) * (width - len(latest_key))
    current_padded = current_key + (0,) * (width - len(current_key))
    return latest_padded > current_padded


async def check_for_update(current: str | None = None) -> UpdateCheckResult:
    """Check PyPI for a newer stable voidx release."""
    current = current or current_version()
    try:
        latest = await fetch_latest_version()
    except Exception as exc:
        log_tool_event("update_check_failed", tool_name="selfupdate", message=f"Update check failed: {exc}")
        return UpdateCheckResult(
            current_version=current,
            latest_version=None,
            update_available=False,
            message=f"Unable to check for updates: {exc}",
            error=str(exc),
        )
    if not latest:
        return UpdateCheckResult(
            current_version=current,
            latest_version=None,
            update_available=False,
            message="Unable to determine the latest voidx version.",
            error="missing version",
        )
    if is_newer(latest, current):
        return UpdateCheckResult(
            current_version=current,
            latest_version=latest,
            update_available=True,
            message=f"voidx {latest} is available (current {current}).",
        )
    return UpdateCheckResult(
        current_version=current,
        latest_version=latest,
        update_available=False,
        message=f"voidx is up to date ({current}).",
    )


async def perform_upgrade(
    version: str | None = None,
    timeout: float = 120.0,
    *,
    current: str | None = None,
) -> UpgradeResult:
    """Explicitly upgrade the Python package in the current virtual environment."""
    current = current or current_version()
    if _launched_by_npm():
        return UpgradeResult(
            ok=False,
            version=None,
            message="This voidx was started by the npm launcher. Run: npm update -g @chikhamx/voidx",
        )
    if not _in_virtualenv():
        return UpgradeResult(
            ok=False,
            version=None,
            message="This Python environment cannot be self-upgraded. Run your package manager manually.",
        )

    target = version
    if target is None:
        check = await check_for_update(current)
        if check.error:
            return UpgradeResult(ok=False, version=None, message=check.message)
        target = check.latest_version
        if not check.update_available:
            return UpgradeResult(ok=True, version=target, message=check.message)
    if target is None:
        return UpgradeResult(ok=False, version=None, message="Unable to determine upgrade target.")
    if _stable_release_key(target) is None:
        return UpgradeResult(ok=False, version=target, message="Upgrade target is not a stable voidx release.")
    if not is_newer(target, current):
        return UpgradeResult(ok=True, version=target, message=f"voidx is already up to date ({current}).")

    env = {
        **os.environ,
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }

    previous = await _verify_installation(None, env, timeout)
    old_version = (
        previous.core_version
        if previous.ok and previous.core_version == previous.cli_version
        else None
    )
    if old_version is None:
        _clear_install_marker()

    target_specs = (f"voidx=={target}", f"voidx-cli=={target}")
    install_result = await _pip_install(target_specs, env, timeout)
    verification = await _verify_installation(target, env, timeout)

    if not verification.ok:
        repair_result = await _pip_install(
            target_specs,
            env,
            timeout,
            force_reinstall=True,
        )
        verification = await _verify_installation(target, env, timeout)
        if not verification.ok:
            failure = verification.message
            if not repair_result.ok:
                failure = repair_result.message
            elif not install_result.ok:
                failure = install_result.message
            failure = _format_upgrade_failure(failure, target)

            if old_version is None:
                return UpgradeResult(
                    ok=False,
                    version=target,
                    message=(
                        f"Upgrade to {target} failed: {failure}. "
                        f"Repair manually: {sys.executable} -m pip install --upgrade "
                        f"voidx=={target} voidx-cli=={target}"
                    ),
                )

            rollback_msg = await _rollback_to(old_version, env, timeout)
            if rollback_msg is not None:
                return UpgradeResult(
                    ok=False,
                    version=target,
                    message=(
                        f"Upgrade to {target} failed: {failure}. "
                        f"Rollback to {old_version} also failed: {rollback_msg}. "
                        f"Repair manually: {sys.executable} -m pip install --upgrade "
                        f"voidx=={target} voidx-cli=={target}"
                    ),
                )
            return UpgradeResult(
                ok=False,
                version=target,
                message=(
                    f"Upgrade to {target} failed: {failure}. "
                    f"Rolled back to {old_version}."
                ),
            )

    _update_install_marker(target)

    return UpgradeResult(
        ok=True,
        version=target,
        message=f"Upgraded voidx to {target}. Restart voidx to use the new version.",
    )


@dataclass(frozen=True)
class _PipResult:
    ok: bool
    message: str


async def _pip_install(
    spec: str | tuple[str, ...],
    env: dict[str, str],
    timeout: float,
    *,
    force_reinstall: bool = False,
) -> _PipResult:
    """Install an exact core/CLI package pair."""
    specs = (spec,) if isinstance(spec, str) else tuple(spec)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-cache-dir",
    ]
    if force_reinstall:
        command.append("--force-reinstall")
    command.extend(specs)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            if process is not None:
                process.kill()
                await process.communicate()
        except Exception as exc:
            log_internal_error(exc, context="selfupdate_kill_timeout")
        return _PipResult(ok=False, message=f"Upgrade timed out after {int(timeout)}s.")
    except Exception as exc:
        return _PipResult(ok=False, message=f"Upgrade failed: {exc}")

    if process.returncode == 0:
        return _PipResult(ok=True, message=f"Installed {' '.join(specs)}.")

    detail = _decode_output(stderr) or _decode_output(stdout) or f"pip exited with {process.returncode}"
    return _PipResult(ok=False, message=f"Upgrade failed: {detail}")


async def _verify_installation(
    expected_version: str | None,
    env: dict[str, str],
    timeout: float,
) -> _VerificationResult:
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _VERIFICATION_PROBE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            if process is not None:
                process.kill()
                await process.communicate()
        except Exception as exc:
            log_internal_error(exc, context="selfupdate_verify_kill_timeout")
        return _VerificationResult(
            ok=False,
            core_version=None,
            cli_version=None,
            message=f"Installation verification timed out after {int(timeout)}s",
        )
    except Exception as exc:
        return _VerificationResult(
            ok=False,
            core_version=None,
            cli_version=None,
            message=f"Installation verification failed: {exc}",
        )

    if process.returncode != 0:
        detail = _decode_output(stderr) or _decode_output(stdout) or f"probe exited with {process.returncode}"
        return _VerificationResult(
            ok=False,
            core_version=None,
            cli_version=None,
            message=f"Installation verification failed: {detail}",
        )

    try:
        payload = json.loads(_decode_output(stdout))
    except Exception as exc:
        return _VerificationResult(
            ok=False,
            core_version=None,
            cli_version=None,
            message=f"Installation verification returned invalid data: {exc}",
        )

    core_version = payload.get("core_version")
    cli_version = payload.get("cli_version")
    failures: list[str] = []
    if not core_version:
        failures.append("voidx is not installed")
    if not cli_version:
        failures.append("voidx-cli is not installed")
    if not payload.get("core_import"):
        failures.append("voidx is not importable")
    if not payload.get("cli_import"):
        failures.append("voidx-cli is not importable")
    if not payload.get("entrypoint_ok"):
        failures.append("voidx entry point failed")
    entrypoint_version = payload.get("entrypoint_version")
    if core_version and cli_version and core_version != cli_version:
        failures.append(f"package versions differ ({core_version} != {cli_version})")
    if core_version and entrypoint_version != core_version:
        failures.append(
            f"entry point version differs ({entrypoint_version or 'missing'} != {core_version})"
        )
    if expected_version is not None:
        if core_version != expected_version:
            failures.append(f"voidx version is {core_version or 'missing'}, expected {expected_version}")
        if cli_version != expected_version:
            failures.append(f"voidx-cli version is {cli_version or 'missing'}, expected {expected_version}")

    if failures:
        return _VerificationResult(
            ok=False,
            core_version=core_version,
            cli_version=cli_version,
            message="; ".join(failures),
        )
    return _VerificationResult(
        ok=True,
        core_version=core_version,
        cli_version=cli_version,
        message=f"Verified voidx and voidx-cli {core_version}",
    )


def _format_upgrade_failure(message: str, target: str) -> str:
    lowered = message.lower()
    locked_on_windows = sys.platform == "win32" and (
        "winerror 32" in lowered
        or "winerror 5" in lowered
        or "permission denied" in lowered
        or "access is denied" in lowered
    )
    if locked_on_windows:
        return (
            f"{message}. Exit voidx, then run the normal installer again: "
            "powershell -File install.ps1"
        )
    return message


def upgrade_hint() -> str:
    if _launched_by_npm():
        return "Run npm update -g @chikhamx/voidx"
    return "Run /upgrade now"


def _stable_release_key(value: str) -> tuple[int, ...] | None:
    normalized = value.strip().lower()
    public = normalized.split("+", 1)[0]
    if _PRE_RELEASE_RE.search(public):
        return None
    match = _RELEASE_RE.match(public)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _in_virtualenv() -> bool:
    return (
        getattr(sys, "real_prefix", None) is not None
        or sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    )


def _launched_by_npm() -> bool:
    return os.environ.get("VOIDX_LAUNCHED_BY_NPM") == "1"


def _install_marker_path() -> Path | None:
    """Return the .voidx-install-version marker path, or None if not in a venv."""
    prefix = getattr(sys, "prefix", None)
    if not prefix:
        return None
    return Path(prefix) / ".voidx-install-version"


def _update_install_marker(new_version: str) -> None:
    """Update the .voidx-install-version marker after a successful upgrade.

    Preserves PBS_TAG and PBS_CPYTHON lines (Python runtime unchanged),
    only replaces the version line. If the marker doesn't exist (non
    install.sh/npm setup), does nothing.
    """
    marker_path = _install_marker_path()
    if marker_path is None or not marker_path.exists():
        return
    try:
        lines = marker_path.read_text(encoding="utf-8").splitlines()
        if len(lines) >= 3:
            lines[0] = new_version
            temp_path = marker_path.with_name(f"{marker_path.name}.{os.getpid()}.tmp")
            try:
                temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.replace(temp_path, marker_path)
            finally:
                temp_path.unlink(missing_ok=True)
    except Exception as exc:
        log_internal_error(exc, context="selfupdate_marker_write")


def _clear_install_marker() -> None:
    marker_path = _install_marker_path()
    if marker_path is None:
        return
    try:
        marker_path.unlink(missing_ok=True)
    except Exception as exc:
        log_internal_error(exc, context="selfupdate_marker_clear")


async def _rollback_to(old_version: str, env: dict[str, str], timeout: float) -> str | None:
    specs = (f"voidx=={old_version}", f"voidx-cli=={old_version}")
    result = await _pip_install(specs, env, timeout)
    if not result.ok:
        return result.message
    verification = await _verify_installation(old_version, env, timeout)
    return None if verification.ok else verification.message


def _decode_output(value: bytes | None) -> str:
    if not value:
        return ""
    return value.decode("utf-8", errors="replace").strip()
