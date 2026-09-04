"""Single source of truth for child-agent mode -> tool capability mapping.

The child registry is physically reduced to the allowlist for its mode, so
the LLM-visible surface and the executable catalog stay identical: a tool the
policy excludes is neither visible nor callable, even via a forged tool call.
"""

from __future__ import annotations

from collections.abc import Iterable

# Parent-orchestration tools never cross into a child registry.
CHILD_BLOCKED_TOOL_IDS = frozenset({
    "agent",
    "agent_control",
    "clarify",
    "checkpoint",
    "workflow",
})

# Audited read-only inspection tools; safe for every child mode.
CHILD_READ_ONLY_TOOL_IDS = frozenset({
    "read",
    "find",
    "search",
    "lsp",
    "document",
    "websearch",
    "webfetch",
})

# Terminal result channel; the child-facing schema is narrowed separately.
CHILD_MESSAGE_TOOL_ID = "message"

_CHILD_SHELL_TOOL_IDS = frozenset({"bash", "powershell"})
_CHILD_WRITE_TOOL_IDS = frozenset({"write", "replace", "manage"})
# External skill/MCP providers may retain process/global mutable state and do not
# yet expose a child-scoped authorization contract; fail closed for child runs.
_CHILD_IMPLEMENT_EXTRA_IDS = frozenset({"todo"})

_REVIEW_ALLOWLIST = CHILD_READ_ONLY_TOOL_IDS | {CHILD_MESSAGE_TOOL_ID}
# debug additionally gets a shell guarded to read-only commands at execution.
_DEBUG_ALLOWLIST = _REVIEW_ALLOWLIST | _CHILD_SHELL_TOOL_IDS
_IMPLEMENT_ALLOWLIST = (
    _DEBUG_ALLOWLIST | _CHILD_WRITE_TOOL_IDS | _CHILD_IMPLEMENT_EXTRA_IDS
)

_ALLOWLIST_BY_MODE = {
    "review": _REVIEW_ALLOWLIST,
    "debug": _DEBUG_ALLOWLIST,
    "implement": _IMPLEMENT_ALLOWLIST,
}


def child_allowed_tool_ids(mode: str, available_ids: Iterable[str]) -> set[str]:
    """Return the child-registry allowlist for ``mode`` ∩ actually-available ids.

    Unknown modes fall back to the most restrictive (review) allowlist.
    """
    allowlist = _ALLOWLIST_BY_MODE.get((mode or "").strip().lower(), _REVIEW_ALLOWLIST)
    return set(available_ids) & set(allowlist)


def mode_allows_guarded_shell(mode: str) -> bool:
    """debug keeps shell tools behind a hard read-only execution guard."""
    return (mode or "").strip().lower() == "debug"
