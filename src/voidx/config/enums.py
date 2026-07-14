"""Configuration enums."""

from __future__ import annotations

from enum import Enum

class SandboxMode(str, Enum):
    """Filesystem boundary control — mirrors Codex CLI sandbox modes.

    read-only:        All write/edit/bash tools are denied.
    workspace-write:  Only writes inside the workspace (+ extra_paths) are allowed.
    danger-full-access: No filesystem restrictions (current voidx behaviour).
    """
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalPolicy(str, Enum):
    """How often voidx asks for human confirmation on tool calls.

    untrusted:   Write/edit/write-capable bash/implement agent tools ask.
    on-failure:  Auto-allow non-bash ask tools, then report failures.
    on-request:  Auto-allow; only ask when the agent explicitly requests approval.
    never:       Full auto — no human-in-the-loop (equivalent to --full-auto).
    """
    UNTRUSTED = "untrusted"
    ON_FAILURE = "on-failure"
    ON_REQUEST = "on-request"
    NEVER = "never"


class PermissionPreset(str, Enum):
    """Ask-first permission presets."""

    READ_ONLY = "read_only"
    SAFE = "safe"
    PROJECT_TRUSTED = "project_trusted"
    FULL_ACCESS = "full_access"


class ApprovalReviewer(str, Enum):
    """Who handles approval prompts when a tool call needs a decision."""
    USER = "user"
    AUTO_REVIEW = "auto_review"


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


class PermissionMode(str, Enum):
    """User-facing presets for sandbox + approval behavior."""
    DEFAULT = "default"
    READ_ONLY = "read-only"
    ACCEPT_EDITS = "accept-edits"
    AUTO_REVIEW = "auto-review"
    FULL_ACCESS = "full-access"
    CUSTOM = "custom"
