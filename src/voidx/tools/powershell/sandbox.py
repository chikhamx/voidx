"""PowerShell sandbox — write target validation and read-only command classification.

Checks Out-File/Set-Content/Add-Content/Tee-Object -FilePath, redirections (> >>),
Remove-Item/Move-Item/Copy-Item targets, and New-Item -ItemType File -Path.
Reuses resolve_safe from tools.base for path validation.
"""

from __future__ import annotations

import re

from voidx.tools.base import ToolContext, resolve_safe

# Read-only PowerShell commands (no write targets, no side effects)
_READ_ONLY_PROGRAMS = frozenset({
    "get-content", "gc", "cat", "type",
    "get-childitem", "gci", "dir", "ls",
    "select-string", "sls",
    "get-item", "gi",
    "get-location", "gl", "pwd",
    "get-process", "gps", "ps",
    "get-service", "gsv",
    "write-output", "echo", "write",
    "get-date", "date",
    "get-help", "help", "man",
    "measure-object", "measure",
    "sort-object", "sort",
    "where-object", "where", "?",
    "foreach-object", "foreach", "%",
    "select-object", "select",
    "format-table", "ft",
    "format-list", "fl",
    "out-host", "out-default",
    "out-string", "out-gridview",
    "get-variable", "gv",
    "get-history", "ghy", "h",
    "test-path",
    "resolve-path",
    "split-path",
    "join-path",
    "get-unique", "gu",
    "group-object", "group",
    "compare-object", "compare",
    "tee-object", "tee",  # tee without -FilePath is read-only
})

# Commands that write to a -FilePath parameter
_FILEPATH_CMDS = {
    "out-file": "filepath",
    "set-content": "path",
    "sc": "path",
    "add-content": "path",
    "ac": "path",
    "tee-object": "filepath",
    "new-item": "path",
    "ni": "path",
}

# Commands with path targets (source/destination)
_PATH_CMDS = {
    "remove-item": "path",
    "del": "path",
    "erase": "path",
    "rd": "path",
    "rmdir": "path",
    "move-item": "destination",
    "mv": "destination",
    "copy-item": "destination",
    "cp": "destination",
    "rename-item": "newname",
    "rni": "newname",
}

_RE_REDIRECT = re.compile(r">>?\s*(\S+)")
_RE_REDIRECT_NAMED = re.compile(r">>?\s*\$null", re.IGNORECASE)


def is_safe_powershell_command(command: str) -> bool:
    """Return True if the command is read-only (no write targets, no side effects)."""
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return True

    # Any redirection means write
    if _RE_REDIRECT.search(stripped):
        return False

    # Subexpressions ($(...)) and array splats (@(...)) can execute arbitrary
    # code — never treat as read-only.
    if re.search(r"[\$@]\(", stripped):
        return False

    # Check for pipeline — if piped, only check the first segment
    # (simplified: if any pipe, check all segments for write commands)
    segments = re.split(r"\s*\|\s*", stripped)
    for segment in segments:
        words = _segment_words(segment)
        if not words:
            continue
        prog = words[0].lower()
        if prog in _FILEPATH_CMDS:
            return False
        if prog in _PATH_CMDS:
            return False
        if prog in ("set-location", "cd", "chdir", "push-location", "pop-location"):
            continue  # cd is safe
        if prog == "git":
            continue  # git read-only commands are safe (write ones blocked elsewhere)
        if prog not in _READ_ONLY_PROGRAMS:
            return False
    return True


def check_sandbox_powershell(
    command: str,
    workspace: str,
    extra_paths: list[str],
) -> str | None:
    """Validate that a PowerShell command's write targets are within the sandbox.

    Returns None if all targets are safe, or a rejection reason.
    Best-effort — catches honest mistakes, not adversarial obfuscation.
    """
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return None

    write_targets: list[str] = []

    # ── redirections (> / >>) ───────────────────────────────────────────
    for m in _RE_REDIRECT.finditer(stripped):
        target = m.group(1)
        if target and not target.lower().startswith("$null"):
            write_targets.append(target)

    # ── -FilePath / -Path / -Destination parameters ─────────────────────
    segments = re.split(r"\s*\|\s*", stripped)
    for segment in segments:
        words = _segment_words(segment)
        if not words:
            continue
        prog = words[0].lower()

        if prog in _FILEPATH_CMDS:
            param_name = _FILEPATH_CMDS[prog]
            target = _extract_param_value(words, param_name)
            if target:
                write_targets.append(target)

        if prog in _PATH_CMDS:
            param_name = _PATH_CMDS[prog]
            target = _extract_param_value(words, param_name)
            if target:
                write_targets.append(target)
            # Also check positional path for remove-item
            if prog in ("remove-item", "del", "erase", "rd", "rmdir"):
                positional = _extract_positional_path(words)
                if positional:
                    write_targets.append(positional)

    # ── check each target ───────────────────────────────────────────────
    blocked_targets: list[str] = []
    for target in write_targets:
        clean = target.strip("'").strip('"').strip("`")
        if not clean:
            continue
        if not resolve_safe(workspace, clean, extra_paths):
            blocked_targets.append(target)

    if blocked_targets:
        return (
            f"SANDBOX: powershell command writes outside the allowed workspace.\n"
            f"  Targets: {', '.join(blocked_targets[:5])}\n"
            f"  Allowed: {workspace}"
            + (f" + {extra_paths}" if extra_paths else "")
        )

    return None


def _sandbox_denial(command: str, ctx: ToolContext) -> str | None:
    """Entry point for tool.py — routes by sandbox_mode."""
    if ctx.sandbox_mode == "danger-full-access":
        return None
    if ctx.sandbox_mode == "read-only":
        if is_safe_powershell_command(command):
            return None
        return f"SANDBOX READ-ONLY: 'powershell' is not allowed.\n  command: {command.strip()[:120]}"
    return check_sandbox_powershell(command, ctx.workspace, ctx.sandbox_extra_paths)


def _segment_words(segment: str) -> list[str]:
    """Split a command segment into tokens (simplified, handles quotes)."""
    words: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    for ch in segment:
        if in_single:
            if ch == "'":
                in_single = False
            else:
                current.append(ch)
        elif in_double:
            if ch == '"':
                in_double = False
            else:
                current.append(ch)
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch.isspace():
                if current:
                    words.append("".join(current))
                    current = []
            else:
                current.append(ch)
    if current:
        words.append("".join(current))
    return words


def _extract_param_value(words: list[str], param_name: str) -> str | None:
    """Extract the value of a -ParamName parameter from token list."""
    for i, w in enumerate(words):
        if w.lower() in (f"-{param_name}", f"-{param_name}:"):
            if i + 1 < len(words):
                return words[i + 1]
        if w.lower().startswith(f"-{param_name}:"):
            return w[len(f"-{param_name}:"):]
    return None


def _extract_positional_path(words: list[str]) -> str | None:
    """Extract the first positional argument (non-flag) after the program name."""
    for w in words[1:]:
        if not w.startswith("-"):
            return w
    return None
