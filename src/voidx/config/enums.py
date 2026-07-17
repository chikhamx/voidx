"""Configuration enums."""

from __future__ import annotations

from enum import Enum

class PermissionMode(str, Enum):
    """Ask-first permission modes."""

    READ_ONLY = "read_only"
    SAFE = "safe"
    AI_APPROVAL = "ai_approval"
    PROJECT_TRUSTED = "project_trusted"
    FULL_ACCESS = "full_access"

    @property
    def sandbox_mode(self) -> str:
        if self == PermissionMode.READ_ONLY:
            return "read-only"
        if self == PermissionMode.FULL_ACCESS:
            return "danger-full-access"
        return "workspace-write"

    @property
    def approval_policy(self) -> str:
        if self == PermissionMode.FULL_ACCESS:
            return "never"
        return "untrusted"


class CodeIde(str, Enum):
    """Preferred app for opening files from the review panel."""
    AUTO = "auto"
    TRAE = "trae"
    CURSOR = "cursor"
    CODE = "code"
    WINDSURF = "windsurf"
    ZED = "zed"
    SUBLIME = "sublime"
    JETBRAINS = "jetbrains"
    GHOSTTY = "ghostty"
    SYSTEM = "system"
