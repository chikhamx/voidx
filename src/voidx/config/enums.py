"""Configuration enums."""

from __future__ import annotations

from enum import Enum, StrEnum

from voidx.tooling.domain.permission import PermissionMode


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


class ReasoningEffort(StrEnum):
    """Unified reasoning intensity for all providers/models."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
