"""Permission types — aligned with opencode PermissionV2."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PermissionMode(str, Enum):
    READ_ONLY = "read_only"
    SAFE = "safe"
    AI_APPROVAL = "ai_approval"
    PROJECT_TRUSTED = "project_trusted"
    FULL_ACCESS = "full_access"

    @property
    def sandbox_mode(self) -> str:
        if self is PermissionMode.READ_ONLY:
            return "read-only"
        if self is PermissionMode.FULL_ACCESS:
            return "danger-full-access"
        return "workspace-write"

    @property
    def approval_policy(self) -> str:
        return "never" if self is PermissionMode.FULL_ACCESS else "untrusted"


class Action(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    DEFER = "defer"
    BLOCKED_ACK = "blocked_ack"


class Rule(BaseModel):
    """A single permission rule.

    permission: tool name or "*" (matches any tool)
    pattern:    wildcard pattern for matching tool arguments (default "*")
    action:     allow | deny | ask
    """
    permission: str
    pattern: str = "*"
    action: Action = Action.ASK


Ruleset = list[Rule]
