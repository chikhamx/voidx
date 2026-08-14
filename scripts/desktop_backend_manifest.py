"""Manifest helpers for the self-contained desktop backend image."""

from __future__ import annotations

import hashlib
import platform
import re
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_VERSION_SOURCE = Path(__file__).resolve().parents[1] / "src/voidx/platform/version.py"
_VERSION_TEXT = _VERSION_SOURCE.read_text(encoding="utf-8")
_VERSION_MATCH = re.search(r'^VERSION = "([^"]+)"', _VERSION_TEXT, re.MULTILINE)
_BACKEND_API_MATCH = re.search(r'^BACKEND_API = "([^"]+)"', _VERSION_TEXT, re.MULTILINE)
BACKEND_VERSION = _VERSION_MATCH.group(1) if _VERSION_MATCH else ""
BACKEND_API = _BACKEND_API_MATCH.group(1) if _BACKEND_API_MATCH else "gateway-v2"


def target_triple(system: str | None = None, machine: str | None = None) -> str:
    """Return the Rust-style target triple for a desktop build target."""
    system_name = (system or platform.system()).strip().lower()
    machine_name = (machine or platform.machine()).strip().lower()

    arch = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "x64": "x86_64",
    }.get(machine_name)
    if arch is None:
        raise ValueError(f"unsupported desktop target architecture: {machine_name or '<empty>'}")

    if system_name in {"darwin", "macos", "mac os x"}:
        return f"{arch}-apple-darwin"
    if system_name == "windows":
        return f"{arch}-pc-windows-msvc"
    if system_name == "linux":
        return f"{arch}-unknown-linux-gnu"
    raise ValueError(f"unsupported desktop target platform: {system or '<empty>'}")


def hash_image_tree(image_root: Path) -> str:
    """Hash image file names and contents in stable order, excluding metadata files."""
    root = image_root.resolve()
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name not in {_MANIFEST_NAME, ".gitkeep"}
    )
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def build_manifest(
    *,
    version: str,
    target: str,
    image_fingerprint: str,
    python_relative: str,
    site_packages_relative: str,
    source_revision: str,
) -> dict[str, Any]:
    """Build the runtime contract embedded in a desktop backend image."""
    if len(image_fingerprint) != 64:
        raise ValueError("image_fingerprint must be a SHA-256 hex digest")
    int(image_fingerprint, 16)
    if not version or not target or not source_revision:
        raise ValueError("version, target, and source_revision must not be empty")
    if not python_relative or not site_packages_relative:
        raise ValueError("runtime-relative paths must not be empty")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backend_version": version,
        "backend_api": BACKEND_API,
        "target": target,
        "image_fingerprint": image_fingerprint,
        "python_relative": python_relative,
        "site_packages_relative": site_packages_relative,
        "source_revision": source_revision,
    }
