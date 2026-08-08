"""PowerShell search route hints for semantic find/search tools."""

from __future__ import annotations

from voidx.tooling.builtin.shell.common import RouteHint


def _value(words: list[str], name: str):
    name = name.lower()
    for i, word in enumerate(words):
        lower = word.lower()
        if lower == name and i + 1 < len(words):
            return words[i + 1]
        if lower.startswith(name + ":"):
            return word[len(name) + 1:]
    return None


def _has_flag(words: list[str], name: str) -> bool:
    name = name.lower()
    return any(word.lower() == name or word.lower().startswith(name + ":") for word in words)


def hint_select_string(words: list[str]) -> RouteHint | None:
    if len(words) < 2 or "|" in words:
        return None
    pattern = _value(words[1:], "-pattern")
    positional = [word for word in words[1:] if not word.startswith("-")]
    if pattern is None and positional:
        pattern = positional[0]
    if not pattern:
        return None
    path = _value(words[1:], "-path")
    if path is None:
        remaining = [word for word in positional if word != pattern]
        if remaining:
            path = remaining[0]
    if path is None or any(ch in path for ch in "*?[]"):
        return None
    if _has_flag(words, "-include") or _has_flag(words, "-exclude") or _has_flag(words, "-literalpath"):
        return None
    if _has_flag(words, "-simplematch"):
        match = "text"
    else:
        match = "regex"
    case = "sensitive" if _has_flag(words, "-casesensitive") else "insensitive"
    context_value = _value(words[1:], "-context")
    context = 0
    if context_value:
        parts = context_value.split(",")
        if len(parts) != 2 or parts[0] != parts[1]:
            return None
        try:
            context = int(parts[0])
        except ValueError:
            return None
    args = {"query": pattern, "path": path, "match": match, "case": case}
    if context:
        args["context"] = context
    return RouteHint(tool_id="search", ui_label="→ search", llm_hint=f"Prefer search tool: {args}", tool_args=args)


def hint_get_child_item(words: list[str]) -> RouteHint | None:
    if len(words) < 2 or "|" in words:
        return None
    if not _has_flag(words, "-file") or not _has_flag(words, "-recurse"):
        return None
    if any(_has_flag(words, flag) for flag in ("-include", "-exclude", "-literalpath")):
        return None
    path = _value(words[1:], "-path")
    if path is None:
        positional = [word for word in words[1:] if not word.startswith("-")]
        path = positional[0] if positional else None
    if path is None or any(ch in path for ch in "*?[]"):
        return None
    pattern = _value(words[1:], "-filter")
    if not pattern:
        return None
    query = None
    extensions = None
    if pattern.startswith("*.") and pattern.count("*") == 1:
        extensions = [pattern[2:]]
    elif pattern.startswith("*") and pattern.endswith("*"):
        query = pattern[1:-1]
    elif pattern.startswith("*"):
        query = pattern[1:]
    elif pattern.endswith("*"):
        query = pattern[:-1]
    else:
        return None
    args = {"path": path, "case": "sensitive"}
    if query:
        args["query"] = query
    if extensions:
        args["extensions"] = extensions
    return RouteHint(tool_id="find", ui_label="→ find", llm_hint=f"Prefer find tool: {args}", tool_args=args)
