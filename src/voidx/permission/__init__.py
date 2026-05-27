"""Permission system — aligned with opencode/Claude Code architecture."""

from voidx.permission.schema import Action, Rule, Ruleset
from voidx.permission.wildcard import match as wildcard_match
from voidx.permission.evaluate import evaluate, from_config, merge
from voidx.permission.service import PermissionService

__all__ = [
    "Action", "Rule", "Ruleset",
    "wildcard_match",
    "evaluate", "from_config", "merge",
    "PermissionService",
]
