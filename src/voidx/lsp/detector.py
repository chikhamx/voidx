"""Auto-detect LSP servers from IDE extensions, package managers, and system paths.

Scans:
  - IDE extensions: VS Code, Cursor, Windsurf, VS Code Insiders
  - npm global packages
  - pip packages
  - Neovim Mason
  - Common system paths (Xcode CLT, Homebrew, etc.)
  - PATH (shutil.which fallback)

Returns LspServerConfig with resolved_command filled in for each found server.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from voidx.logging import log_internal_error
from voidx.lsp.detector_data import (
    LANGUAGE_DEFAULTS,
    _EXTENSION_MAP,
    _EXTRA_PATH_GLOBS,
    _MASON_MAP,
    _NPM_PACKAGE_MAP,
    _PIP_PACKAGE_MAP,
)
from voidx.lsp.domain import LspServerConfig


def detect_servers() -> dict[str, LspServerConfig]:
    """Run all detectors and return merged results.

    Earlier detectors take priority. Returns {language: LspServerConfig} with
    resolved_command filled in.
    """
    results: dict[str, LspServerConfig] = {}

    for detector in [
        _detect_ide_extensions,
        _detect_npm_global,
        _detect_pip,
        _detect_mason,
        _detect_extra_paths,
        _detect_path,
    ]:
        for language, config in detector().items():
            if language not in results:
                results[language] = config

    return results


def resolve_command(command: str) -> str:
    """Return the absolute path of *command* if found on PATH, else ''."""
    if not command:
        return ""
    path = Path(command)
    if path.is_absolute() and path.exists():
        return str(path)
    resolved = shutil.which(command)
    return resolved or ""


# ---------------------------------------------------------------------------
# Detector helpers
# ---------------------------------------------------------------------------


def _find_extensions_dirs() -> list[Path]:
    """Return all IDE extension directories that exist."""
    candidates = []
    home = Path.home()

    # Standard locations (macOS + Linux; Windows handled via APPDATA/USERPROFILE)
    for dir_name in (".vscode", ".cursor", ".vscode-insiders", ".vscode-oss"):
        p = home / dir_name
        if p.is_dir():
            candidates.append(p)

    # Windsurf
    p = home / ".windsurf"
    if p.is_dir():
        candidates.append(p)

    # Windows: %USERPROFILE%\.vscode
    if sys.platform == "win32":
        for env_var in ("USERPROFILE", "APPDATA"):
            base = os.environ.get(env_var)
            if base:
                for dir_name in (".vscode", ".cursor", ".vscode-insiders"):
                    p = Path(base) / dir_name
                    if p.is_dir():
                        candidates.append(p)

    return candidates


def _iter_extensions(ext_dirs: list[Path]) -> list[tuple[Path, str]]:
    """Yield (extension_root, extension_id) for every extension found."""
    extensions: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for ext_dir in ext_dirs:
        path = ext_dir / "extensions"
        if not path.is_dir():
            continue
        for child in path.iterdir():
            if child.is_dir():
                ext_id = child.name
                # Strip version suffix like "1.0.10" → just the publisher.name
                base_id = _strip_semver_suffix(ext_id)
                if base_id not in seen:
                    seen.add(base_id)
                    extensions.append((child, base_id))
    return extensions


def _strip_semver_suffix(name: str) -> str:
    """Strip trailing -x.y.z or -x.y.z-suffix from an extension folder name."""
    parts = name.rsplit("-", 1)
    if len(parts) == 2 and _looks_like_semver(parts[1]):
        return parts[0]
    # Try double-split for -universal, -darwin-arm64 etc.
    parts = name.rsplit("-", 2)
    if len(parts) >= 2 and _looks_like_semver(parts[1]):
        return parts[0]
    return name


def _looks_like_semver(s: str) -> bool:
    """Check if string looks like x.y.z[.w]."""
    segs = s.split(".")
    if len(segs) < 2:
        return False
    return all(part.lstrip("0123456789") == "" for part in segs[: min(len(segs), 4)])


def _read_package_json(ext_path: Path) -> dict[str, Any] | None:
    """Parse extension's package.json."""
    pkg_path = ext_path / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        return json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _node_bin() -> str:
    """Return the path to node, or ''."""
    return shutil.which("node") or ""


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------


def _detect_ide_extensions() -> dict[str, LspServerConfig]:
    """Scan IDE extension directories for bundled LSP servers."""
    results: dict[str, LspServerConfig] = {}

    ext_dirs = _find_extensions_dirs()
    if not ext_dirs:
        return results

    for ext_path, ext_id in _iter_extensions(ext_dirs):
        for prefix, language, rel_path, args, label in _EXTENSION_MAP:
            if ext_id == prefix:
                server_path = ext_path / rel_path
                if not server_path.exists():
                    continue
                command = str(server_path.resolve())
                # JS-based servers need node
                if rel_path.endswith(".js"):
                    node = _node_bin()
                    if not node:
                        continue
                    args = [str(server_path.resolve()), *args]
                    command = node

                config = LspServerConfig(
                    language=language,
                    command=command,
                    args=args,
                    extensions=LANGUAGE_DEFAULTS.get(language, LspServerConfig(language=language, command=command)).extensions,
                    enabled=True,
                    resolved_command=command,
                    detected_source=label,
                )
                results[language] = config

    return results


def _detect_npm_global() -> dict[str, LspServerConfig]:
    """Detect LSP servers installed via npm global."""
    results: dict[str, LspServerConfig] = {}

    # Find npm global prefix
    npm_prefixes: list[str] = []

    # standard npm global
    for cmd in ("npm",):
        try:
            import subprocess
            out = subprocess.check_output([cmd, "root", "-g"], text=True, timeout=5).strip()
            if out and Path(out).is_dir():
                npm_prefixes.append(out)
        except Exception as exc:
            log_internal_error(exc, context="lsp_npm_prefix_detect")

    # nvm
    nvm_dir = os.environ.get("NVM_DIR") or os.path.expanduser("~/.nvm")
    for nvm_path in Path(nvm_dir).glob("versions/node/*/lib/node_modules"):
        if nvm_path.is_dir():
            npm_prefixes.append(str(nvm_path))

    # fnm
    fnm_base = os.path.expanduser("~/Library/Application Support/fnm/node-versions")
    if sys.platform == "darwin":
        for fnm_path in Path(fnm_base).glob("*/installation/lib/node_modules"):
            if fnm_path.is_dir():
                npm_prefixes.append(str(fnm_path))

    # Homebrew node
    brew_paths = ["/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules"]
    npm_prefixes.extend(p for p in brew_paths if Path(p).is_dir())

    for prefix in npm_prefixes:
        for pkg_name, (language, command, args) in _NPM_PACKAGE_MAP.items():
            pkg_dir = Path(prefix) / pkg_name
            if not pkg_dir.is_dir():
                continue
            bin_dir = pkg_dir / "bin"
            if bin_dir.is_dir():
                bin_path = bin_dir / command
                if bin_path.is_file():
                    resolved = str(bin_path.resolve())
                else:
                    resolved = resolve_command(command)
            else:
                resolved = resolve_command(command)

            if not resolved and pkg_name == "pyright":
                # Pyright bundles a node server we can run directly
                server_js = pkg_dir / "dist" / "server.js"
                node = _node_bin()
                if server_js.is_file() and node:
                    resolved = node
                    args = [str(server_js.resolve()), *args]

            if resolved:
                results[language] = LspServerConfig(
                    language=language,
                    command=resolved,
                    args=args,
                    extensions=LANGUAGE_DEFAULTS.get(language, LspServerConfig(language=language, command=command)).extensions,
                    enabled=True,
                    resolved_command=resolved,
                    detected_source=f"npm global ({pkg_name})",
                )

    return results


def _detect_pip() -> dict[str, LspServerConfig]:
    """Detect LSP servers installed via pip/pipx."""
    results: dict[str, LspServerConfig] = {}

    for python in ("python3", "python"):
        python_path = shutil.which(python)
        if not python_path:
            continue
        try:
            import subprocess
            out = subprocess.check_output(
                [python_path, "-m", "pip", "list", "--format=json"],
                timeout=10,
            ).decode("utf-8", errors="replace")
            packages = json.loads(out)
        except Exception:
            continue

        installed: set[str] = set()
        for pkg in packages:
            name = pkg.get("name", "").lower()
            if name in _PIP_PACKAGE_MAP:
                installed.add(name)

        for pkg_name in installed:
            language, command, args = _PIP_PACKAGE_MAP[pkg_name]
            resolved = resolve_command(command)
            if resolved:
                results[language] = LspServerConfig(
                    language=language,
                    command=resolved,
                    args=args,
                    extensions=LANGUAGE_DEFAULTS.get(language, LspServerConfig(language=language, command=command)).extensions,
                    enabled=True,
                    resolved_command=resolved,
                    detected_source=f"pip ({pkg_name})",
                )

        if installed:
            break  # only check one python

    return results


def _detect_mason() -> dict[str, LspServerConfig]:
    """Detect LSP servers installed via Neovim Mason."""
    results: dict[str, LspServerConfig] = {}

    mason_paths = [
        Path.home() / ".local" / "share" / "nvim" / "mason" / "packages",
        Path.home() / ".local" / "share" / "nvim" / "lazy" / "mason" / "packages",
    ]
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            mason_paths.append(Path(local_appdata) / "nvim-data" / "mason" / "packages")

    for mason_dir in mason_paths:
        if not mason_dir.is_dir():
            continue
        for pkg_name, (language, command, args) in _MASON_MAP.items():
            pkg_dir = mason_dir / pkg_name
            if not pkg_dir.is_dir():
                continue
            # Mason puts binaries directly in the package dir
            bin_path = pkg_dir / command
            if bin_path.is_file():
                resolved = str(bin_path.resolve())
            else:
                resolved = resolve_command(command)
            if resolved:
                results[language] = LspServerConfig(
                    language=language,
                    command=resolved,
                    args=args,
                    extensions=LANGUAGE_DEFAULTS.get(language, LspServerConfig(language=language, command=command)).extensions,
                    enabled=True,
                    resolved_command=resolved,
                    detected_source=f"Mason ({pkg_name})",
                )

    return results


def _detect_extra_paths() -> dict[str, LspServerConfig]:
    """Find LSP binaries in known absolute paths (Xcode CLT, Homebrew, etc.)."""
    results: dict[str, LspServerConfig] = {}

    for path_globs, binary, language, args in _EXTRA_PATH_GLOBS:
        for pattern in path_globs:
            for p in Path(pattern).glob(binary):
                if p.is_file():
                    resolved = str(p.resolve())
                    results[language] = LspServerConfig(
                        language=language,
                        command=resolved,
                        args=args,
                        extensions=LANGUAGE_DEFAULTS.get(language, LspServerConfig(language=language, command=resolved)).extensions,
                        enabled=True,
                        resolved_command=resolved,
                        detected_source=f"system ({resolved})",
                    )
                    break
            else:
                continue
            break

    return results


def _detect_path() -> dict[str, LspServerConfig]:
    """Fallback: scan PATH with shutil.which for known command names."""
    results: dict[str, LspServerConfig] = {}

    for language, default in LANGUAGE_DEFAULTS.items():
        if not default.command:
            continue
        resolved = resolve_command(default.command)
        if resolved:
            config = default.model_copy(deep=True)
            config.command = resolved
            config.resolved_command = resolved
            config.detected_source = "PATH"
            results[language] = config

    return results
