"""Version check and explicit self-update helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from voidx import __version__
from voidx.logging import log_internal_error
from voidx.logging.tool_log import log_tool_event

logger = logging.getLogger(__name__)

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


def is_newer(latest: str, current: str = __version__) -> bool:
    """Return True when latest is a newer stable release than current."""
    latest_key = _stable_release_key(latest)
    current_key = _stable_release_key(current)
    if latest_key is None or current_key is None:
        return False
    width = max(len(latest_key), len(current_key))
    latest_padded = latest_key + (0,) * (width - len(latest_key))
    current_padded = current_key + (0,) * (width - len(current_key))
    return latest_padded > current_padded


async def check_for_update(current: str = __version__) -> UpdateCheckResult:
    """Check PyPI for a newer stable voidx release."""
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


async def perform_upgrade(version: str | None = None, timeout: float = 120.0) -> UpgradeResult:
    """Explicitly upgrade the Python package in the current virtual environment."""
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
        check = await check_for_update()
        if check.error:
            return UpgradeResult(ok=False, version=None, message=check.message)
        target = check.latest_version
        if not check.update_available:
            return UpgradeResult(ok=True, version=target, message=check.message)
    if target is None:
        return UpgradeResult(ok=False, version=None, message="Unable to determine upgrade target.")
    if _stable_release_key(target) is None:
        return UpgradeResult(ok=False, version=target, message="Upgrade target is not a stable voidx release.")
    if not is_newer(target, __version__):
        return UpgradeResult(ok=True, version=target, message=f"voidx is already up to date ({__version__}).")

    old_version = _installed_version("voidx") or __version__

    env = {
        **os.environ,
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }

    # Step 1: upgrade voidx core (failure is fatal)
    core_result = await _pip_install(f"voidx=={target}", env, timeout)
    if not core_result.ok:
        return UpgradeResult(ok=False, version=target, message=core_result.message)

    # Step 2: upgrade voidx-cli (failure triggers rollback)
    cli_result = await _pip_install(f"voidx-cli=={target}", env, timeout)
    if not cli_result.ok:
        log_tool_event(
            "upgrade_voidx_cli_failed",
            tool_name="selfupdate",
            message=f"voidx-cli upgrade failed: {cli_result.message}",
        )

    # Step 3: verify voidx-cli is importable
    if not _can_import_voidx_cli():
        rollback_msg = await _rollback_to(old_version, env, timeout)
        if rollback_msg is not None:
            return UpgradeResult(
                ok=False,
                version=target,
                message=(
                    f"Upgrade to {target} failed: voidx-cli is not available. "
                    f"Rolled back to {old_version}, but rollback encountered an error: {rollback_msg}. "
                    f"Fix manually: pip install voidx-cli=={target}"
                ),
            )
        return UpgradeResult(
            ok=False,
            version=target,
            message=(
                f"Upgrade to {target} failed: voidx-cli is not available. "
                f"Rolled back to {old_version}. Please retry later or install voidx-cli manually: "
                f"pip install voidx-cli=={target}"
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


async def _pip_install(spec: str, env: dict[str, str], timeout: float) -> _PipResult:
    """Run pip install --upgrade for a single package spec."""
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-cache-dir",
        spec,
    ]
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
        return _PipResult(ok=True, message=f"Installed {spec}.")

    detail = _decode_output(stderr) or _decode_output(stdout) or f"pip exited with {process.returncode}"
    return _PipResult(ok=False, message=f"Upgrade failed: {detail}")


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


def _installed_version(package: str) -> str | None:
    """Return the currently installed version of a package, or None."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version(package)
        except PackageNotFoundError:
            return None
    except Exception:
        return None


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
        # Marker format: line 0 = version, line 1 = PBS_TAG, line 2 = PBS_CPYTHON.
        # Only update when all three lines exist to avoid corrupting partial files.
        if len(lines) >= 3:
            lines[0] = new_version
            marker_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.debug("failed to update install marker at %s: %s", marker_path, exc)


def _can_import_voidx_cli() -> bool:
    """Check whether voidx-cli is installed and importable.

    Uses importlib.metadata to detect the package (re-reads .dist-info
    from site-packages on every call, so it reflects subprocess pip
    installs), then confirms the module is actually importable.
    Metadata-only checks miss interrupted installs where .dist-info
    exists but .py files are missing.
    """
    try:
        from importlib import import_module
        from importlib.metadata import PackageNotFoundError, version
        try:
            if version("voidx-cli") is None:
                return False
        except PackageNotFoundError:
            return False
        try:
            import_module("voidx_cli")
        except ImportError:
            return False
        return True
    except Exception:
        return False


async def _rollback_to(old_version: str, env: dict[str, str], timeout: float) -> str | None:
    """Roll back voidx and voidx-cli to old_version.

    Returns None on success, or an error message if rollback failed.
    """
    core_result = await _pip_install(f"voidx=={old_version}", env, timeout)
    if not core_result.ok:
        return core_result.message
    # Best-effort: try to restore voidx-cli too (may not exist for old versions)
    await _pip_install(f"voidx-cli=={old_version}", env, timeout)
    return None


def _decode_output(value: bytes | None) -> str:
    if not value:
        return ""
    return value.decode("utf-8", errors="replace").strip()
