"""Sandbox mode path validation — filesystem boundary enforcement."""

from __future__ import annotations

import shlex
from pathlib import Path

from voidx.permission.constants import (
    FS_WRITE_COMMANDS,
    GIT_GLOBAL_OPTIONS_WITH_VALUE,
    REDIR_PATTERNS,
)


def _allowed(
    path: str,
    workspace: str,
    extra_paths: list[str],
    current_dir: Path | None = None,
) -> bool:
    """Check if the resolved path is inside workspace or extra_paths."""
    try:
        raw = Path(path).expanduser()
        if raw.is_absolute():
            resolved = raw.resolve()
        else:
            base = current_dir if current_dir is not None else Path(workspace)
            resolved = (base / raw).resolve()
    except (OSError, ValueError):
        return True  # unresolvable → don't block, let the tool report errors

    allowed = [Path(workspace).resolve()]
    for ep in extra_paths:
        allowed.append(Path(ep).expanduser().resolve())

    for base in allowed:
        try:
            if resolved == base or resolved.is_relative_to(base):
                return True
        except (ValueError, OSError):
            continue
    return False


def check_sandbox_filepath(
    file_path: str,
    workspace: str,
    extra_paths: list[str],
) -> str | None:
    """Validate that file_path is inside the allowed workspace + extra_paths.

    Returns None if the path is allowed, or a human-readable rejection reason.
    Invalid / unresolvable paths are NOT blocked (let the tool itself report
    the error).
    """
    if _allowed(file_path, workspace, extra_paths):
        return None

    return (
        f"SANDBOX: '{file_path}' is outside the allowed workspace.\n"
        f"  Allowed: {workspace}"
        + (f" + {extra_paths}" if extra_paths else "")
    )





def check_sandbox_bash(
    command: str,
    workspace: str,
    extra_paths: list[str],
) -> str | None:
    """Validate that a bash command's write targets are within the sandbox.

    Extracts redirect targets, tee targets, and destructive file ops.
    Returns None if all targets are safe, or a rejection reason.

    This is a *best-effort* check — sophisticated command obfuscation can
    bypass it.  It is designed to catch honest mistakes, not adversarial
    attacks.  The hard blocklist in bash.py (_BLOCKED) covers the really
    dangerous stuff.
    """
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return None

    write_targets: list[str] = []

    # ── redirections (> / >> / | tee) ────────────────────────────────
    for pattern in REDIR_PATTERNS:
        for m in pattern.finditer(stripped):
            target = m.group(1)
            if target and not target.startswith("&") and not target.startswith("/dev/"):
                write_targets.append(target)

    # ── destructive commands (rm/cp/mv/…) ─────────────────────────────
    words = _shell_words(stripped)
    prog = _program(words).lower() if words else ""
    segment_targets = _extract_segment_targets(words, workspace)
    write_targets.extend(segment_targets)

    # ── git push remote (non-force is blocked by sandbox if workspace-write) ──
    if _is_git_push_outside(prog, words, workspace, extra_paths):
        return (
            f"SANDBOX: git push writes outside the allowed workspace."
            f"\n  Allowed: {workspace}"
            + (f" + {extra_paths}" if extra_paths else "")
            + "\n  Use /permission full_access to allow git push."
        )

    # ── check each target ────────────────────────────────────────────
    blocked_targets: list[str] = []
    for target in write_targets:
        # Remove surrounding quotes if present
        clean = target.strip('"').strip("'")
        if not clean or clean.startswith("/dev/"):
            continue
        if not _allowed(clean, workspace, extra_paths):
            blocked_targets.append(target)

    if blocked_targets:
        return (
            f"SANDBOX: bash command writes outside the allowed workspace.\n"
            f"  Targets: {', '.join(blocked_targets[:5])}\n"
            f"  Allowed: {workspace}"
            + (f" + {extra_paths}" if extra_paths else "")
        )

    return None


def _shell_words(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def _extract_segment_targets(words: list[str], workspace: str) -> list[str]:
    targets: list[str] = []
    current_dir = Path(workspace).resolve()
    segment: list[str] = []
    for token in [*words, ";"]:
        if token in (";", "&&", "||"):
            targets.extend(_extract_command_targets(segment, workspace, current_dir))
            if segment and _program(segment).lower() == "cd":
                next_dir = _first_non_flag_arg(segment[1:])
                if next_dir:
                    raw = Path(next_dir.strip('"').strip("'")).expanduser()
                    current_dir = (raw if raw.is_absolute() else current_dir / raw).resolve()
            segment = []
            continue
        if token in ("|", "||", "&&"):
            targets.extend(_extract_command_targets(segment, workspace, current_dir))
            segment = []
            continue
        segment.append(token)
    return targets


def _extract_command_targets(words: list[str], workspace: str, current_dir: Path) -> list[str]:
    if not words:
        return []
    prog = _program(words).lower()
    result: list[str] = []

    for index, word in enumerate(words[:-1]):
        if word in (">", ">>"):
            result.extend(_resolve_targets([words[index + 1]], current_dir))
    for word in words:
        if word.startswith("of=") and len(word) > 3:
            result.extend(_resolve_targets([word[3:]], current_dir))

    arg_idx = FS_WRITE_COMMANDS.get(prog)
    if arg_idx is None:
        return result
    if arg_idx == 0:
        return result

    args = _program_args(words)
    raw_targets: list[str] = []
    if arg_idx > 0:
        raw_targets = [w for w in args if not w.startswith("-")]
    elif arg_idx == -1 and len(args) >= 2:
        dest = args[-1]
        if not dest.startswith("-"):
            raw_targets = [dest]

    result.extend(_resolve_targets(raw_targets, current_dir))
    return result


def _resolve_targets(raw_targets: list[str], current_dir: Path) -> list[str]:
    result: list[str] = []
    for target in raw_targets:
        clean = target.strip('"').strip("'")
        if not clean or clean.startswith("&") or clean.startswith("/dev/"):
            continue
        path = Path(clean).expanduser()
        result.append(str(path if path.is_absolute() else current_dir / path))
    return result


def _program(words: list[str]) -> str:
    for word in words:
        if "=" in word and not word.startswith("=") and word.split("=", 1)[0].isidentifier():
            continue
        return word
    return ""


def _program_args(words: list[str]) -> list[str]:
    prog_seen = False
    args: list[str] = []
    for word in words:
        if not prog_seen:
            if "=" in word and not word.startswith("=") and word.split("=", 1)[0].isidentifier():
                continue
            prog_seen = True
            continue
        args.append(word)
    return args


def _first_non_flag_arg(words: list[str]) -> str:
    for word in words:
        if not word.startswith("-"):
            return word
    return ""


def _is_git_push_outside(
    prog: str,
    words: list[str],
    workspace: str,
    extra_paths: list[str],
) -> bool:
    """Check if git push modifies something beyond the workspace.

    In workspace-write mode, git push to any remote can write outside the
    local filesystem. Extra local write paths do not authorize remote writes.
    """
    if prog != "git" or len(words) < 2:
        return False
    subcommand = _git_subcommand(_program_args(words))
    return subcommand == "push"




def _git_subcommand(args: list[str]) -> str:
    index = 0
    while index < len(args):
        word = args[index]
        if word in GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(word.startswith(f"{option}=") for option in GIT_GLOBAL_OPTIONS_WITH_VALUE if option.startswith("--")):
            index += 1
            continue
        if word == "--":
            index += 1
            continue
        if word.startswith("-"):
            index += 1
            continue
        return word
    return ""


# ── export ───────────────────────────────────────────────────────────

__all__ = ["check_sandbox_filepath", "check_sandbox_bash"]
