"""Permission types — aligned with opencode PermissionV2."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


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
