"""Centralized path constants for voidx home and workspace-relative files.

Single source of truth for ``~/.voidx`` (global) and ``.voidx/...``
(workspace-relative) paths. Import from here instead of spelling
``Path.home() / ".voidx"`` or ``".voidx/..."`` literals.
"""

from __future__ import annotations

from pathlib import Path

VOIDX_DIR_NAME = ".voidx"


def voidx_home() -> Path:
    """Global voidx directory under the user home (``~/.voidx``)."""
    return Path.home() / VOIDX_DIR_NAME


def voidx_logs_dir() -> Path:
    """Global logs directory (``~/.voidx/logs``)."""
    return voidx_home() / "logs"


def voidx_global_skills_dir() -> Path:
    """Global skills directory (``~/.voidx/skills``)."""
    return voidx_home() / "skills"


def voidx_workspace_dir(workspace: str | Path = ".") -> Path:
    """Workspace-local voidx directory (``<workspace>/.voidx``)."""
    return Path(workspace).resolve() / VOIDX_DIR_NAME


def voidx_workspace_skills_dir(workspace: str | Path = ".") -> Path:
    """Workspace-local skills directory (``<workspace>/.voidx/skills``)."""
    return voidx_workspace_dir(workspace) / "skills"


# Workspace-relative file names (kept as strings for config loaders that
# resolve against an arbitrary workspace root).
SETTINGS_FILE = f"{VOIDX_DIR_NAME}/settings.json"
SKILLS_STATE_FILE = f"{VOIDX_DIR_NAME}/skills.json"
LSP_CONFIG_FILE = f"{VOIDX_DIR_NAME}/lsp.json"
CLIPBOARD_ATTACHMENT_DIR = f"{VOIDX_DIR_NAME}/attachments"
