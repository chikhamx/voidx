"""Permission types — aligned with opencode PermissionV2."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Action = Literal["allow", "deny", "ask", "defer", "blocked_ack"]


class Rule(BaseModel):
    """A single permission rule.

    permission: tool name or "*" (matches any tool)
    pattern:    wildcard pattern for matching tool arguments (default "*")
    action:     allow | deny | ask
    """
    permission: str
    pattern: str = "*"
    action: Action = "ask"


Ruleset = list[Rule]
