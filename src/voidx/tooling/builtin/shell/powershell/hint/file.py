"""File operation route hints — Get-Content → read, Out-File/Set-Content → write."""

from __future__ import annotations

from voidx.tooling.builtin.shell.common import RouteHint


def hint_get_content(words: list[str]) -> RouteHint | None:
    """Get-Content / cat / type → read tool."""
    if len(words) < 2:
        return None
    args = words[1:]
    # Skip flags like -TotalCount, -Tail, -Path
    file_arg = None
    for a in args:
        if not a.startswith("-"):
            file_arg = a
            break
    if not file_arg:
        return None
    return RouteHint(
        tool_id="read",
        ui_label="→ read",
        llm_hint=f"Prefer read tool for {file_arg!r} — structured output with line numbers.",
    )


def hint_out_file(words: list[str]) -> RouteHint | None:
    """Out-File / Set-Content / Add-Content → write tool."""
    if len(words) < 2:
        return None
    # Find -FilePath or -Path parameter value, or positional arg
    file_arg = None
    for i, w in enumerate(words[1:], 1):
        if w.lower() in ("-filepath", "-path") and i + 1 < len(words):
            file_arg = words[i + 1]
            break
        if w.lower().startswith("-filepath:"):
            file_arg = w[len("-filepath:"):]
            break
        if w.lower().startswith("-path:"):
            file_arg = w[len("-path:"):]
            break
    if not file_arg:
        return None
    return RouteHint(
        tool_id="write",
        ui_label="→ write",
        llm_hint=f"Prefer write tool for {file_arg!r} — precise line insertion.",
    )
