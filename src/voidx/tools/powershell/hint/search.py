"""Search route hints — Select-String → grep, Get-ChildItem → glob."""

from __future__ import annotations

from voidx.tools.shell.common import RouteHint


def _hint_select_string(words: list[str]) -> RouteHint | None:
    """Select-String / sls → grep tool."""
    if len(words) < 2:
        return None
    pattern = None
    for i, w in enumerate(words[1:], 1):
        if w.lower() in ("-pattern",) and i + 1 < len(words):
            pattern = words[i + 1]
            break
        if w.lower().startswith("-pattern:"):
            pattern = w[len("-pattern:"):]
            break
    if not pattern:
        # First positional arg is the pattern
        for w in words[1:]:
            if not w.startswith("-"):
                pattern = w
                break
    if not pattern:
        return None
    return RouteHint(
        tool_id="grep",
        ui_label="→ grep",
        llm_hint=f"Prefer grep tool for pattern {pattern!r} — structured matches with context.",
    )


def _hint_get_child_item(words: list[str]) -> RouteHint | None:
    """Get-ChildItem / dir / ls / gci → glob tool."""
    if len(words) < 2:
        return None
    path = None
    for w in words[1:]:
        if not w.startswith("-"):
            path = w
            break
    if not path:
        return None
    return RouteHint(
        tool_id="glob",
        ui_label="→ glob",
        llm_hint=f"Prefer glob tool for {path!r} — sorted file listing.",
    )
