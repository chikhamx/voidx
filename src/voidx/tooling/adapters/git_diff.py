"""Git-backed diff queries."""

from __future__ import annotations

import logging
import subprocess


_logger = logging.getLogger(__name__)


def log_tool_event(event: str, *, tool_name: str = "", message: str = "", **kwargs: object) -> None:
    _logger.warning("%s tool=%s message=%s extra=%s", event, tool_name, message, kwargs)


def git_diff(workspace: str, staged: bool = False) -> str:
    try:
        args = ["git", "diff"]
        if staged:
            args.append("--staged")
        result = subprocess.run(args, capture_output=True, text=True, cwd=workspace, timeout=10)
        return result.stdout
    except Exception as exc:
        log_tool_event("diffing_git_diff", tool_name="git", message=str(exc))
        return ""


def git_diff_stat(workspace: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"], capture_output=True, text=True, cwd=workspace, timeout=10
        )
        return result.stdout.strip()
    except Exception as exc:
        log_tool_event("diffing_git_diff_stat", tool_name="git", message=str(exc))
        return ""
