"""Permission evaluation — core algorithm, config parsing, rule merging.

Aligned with opencode/core/permission.ts:
  - evaluate(): findLast matching rule across merged rulesets
  - from_config(): YAML-like dict → Ruleset
  - merge(): concatenate rulesets
"""

from __future__ import annotations

import os
from pathlib import Path

from voidx.permission.schema import Action, Rule, Ruleset
from voidx.permission.wildcard import match as wildcard_match


def evaluate(permission: str, pattern: str, *rulesets: Ruleset) -> Rule:
    """Evaluate permission: find the last matching rule across all rulesets.

    Later rulesets override earlier ones (merge order).
    Within a ruleset, later rules override earlier ones (findLast).

    Returns a Rule with action="ask" if no rule matches (default-deny-lite).
    """
    all_rules: list[Rule] = []
    for rs in rulesets:
        all_rules.extend(rs)

    for rule in reversed(all_rules):
        if wildcard_match(permission, rule.permission) and wildcard_match(pattern, rule.pattern):
            return rule

    # Default: ask if no rule matches
    return Rule(permission=permission, pattern=pattern, action="ask")


def from_config(config: dict) -> Ruleset:
    """Convert a nested dictionary config to a flat Ruleset.

    Config format (matches opencode ConfigPermission.Info):
      {
        "*": "allow",                          → Rule("*", "*", "allow")
        "bash": {"git push*": "ask"},          → Rule("bash", "git push*", "ask")
        "read": {"*": "allow", "*.env": "ask"}, → Rule("read", "*", "allow"), Rule("read", "*.env", "ask")
        "external_directory": {"*": "ask", "~/.voidx/*": "allow"},
        "edit": "deny",                        → Rule("edit", "*", "deny")
      }
    """
    ruleset: Ruleset = []
    for key, value in config.items():
        if isinstance(value, str):
            # Simple: "tool": "action"
            ruleset.append(Rule(permission=key, pattern="*", action=_parse_action(value)))
        elif isinstance(value, dict):
            # Nested: "tool": {"pattern": "action", ...}
            for pattern, action in value.items():
                ruleset.append(Rule(
                    permission=key,
                    pattern=_expand_path(pattern),
                    action=_parse_action(action),
                ))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    ruleset.append(Rule(permission=key, pattern="*", action=_parse_action(item)))
                elif isinstance(item, dict):
                    ruleset.append(Rule(
                        permission=key,
                        pattern=_expand_path(item.get("pattern", "*")),
                        action=_parse_action(item.get("action", "ask")),
                    ))
    return ruleset


def merge(*rulesets: Ruleset) -> Ruleset:
    """Merge multiple rulesets. Later ones override earlier ones."""
    result: Ruleset = []
    for rs in rulesets:
        result.extend(rs)
    return result


def disabled_tools(all_tools: list[str], ruleset: Ruleset) -> set[str]:
    """Find which tools are completely disabled (denied with pattern="*")."""
    EDIT_TOOLS = {"manage", "write", "replace"}
    disabled: set[str] = set()
    for tool in all_tools:
        permission = "edit" if tool in EDIT_TOOLS else tool
        rule = evaluate(permission, "*", ruleset)
        if rule.action == "deny" and rule.pattern == "*":
            disabled.add(tool)
    return disabled


def _parse_action(value: str | bool) -> Action:
    if isinstance(value, bool):
        return "allow" if value else "deny"
    if value in ("allow", "deny", "ask"):
        return value  # type: ignore[return-value]
    raise ValueError(f"Invalid permission action: {value}")


def _expand_path(pattern: str) -> str:
    """Expand ~ and $HOME in path patterns."""
    if pattern.startswith("~/"):
        return str(Path.home() / pattern[2:])
    if pattern == "~":
        return str(Path.home())
    if pattern.startswith("$HOME/"):
        return str(Path.home() / pattern[6:])
    if pattern.startswith("$HOME"):
        return str(Path.home() / pattern[5:])
    return pattern
