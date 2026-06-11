"""Version check and explicit self-update helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from voidx import __version__

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
        logger.debug("Update check failed", exc_info=True)
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

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-cache-dir",
        f"voidx=={target}",
    ]
    env = {
        **os.environ,
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }
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
        except Exception:
            pass
        return UpgradeResult(ok=False, version=target, message=f"Upgrade timed out after {int(timeout)}s.")
    except Exception as exc:
        return UpgradeResult(ok=False, version=target, message=f"Upgrade failed: {exc}")

    if process.returncode == 0:
        return UpgradeResult(
            ok=True,
            version=target,
            message=f"Upgraded voidx to {target}. Restart voidx to use the new version.",
        )

    detail = _decode_output(stderr) or _decode_output(stdout) or f"pip exited with {process.returncode}"
    return UpgradeResult(ok=False, version=target, message=f"Upgrade failed: {detail}")


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


def _decode_output(value: bytes | None) -> str:
    if not value:
        return ""
    return value.decode("utf-8", errors="replace").strip()
