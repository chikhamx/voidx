"""Git tool result construction helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from voidx.tools.base import ToolContext, ToolResult, tool_timeout_metadata

from voidx.tools.git.models import GitRepo


def _parse_track(track: str) -> tuple[int, int]:
    ahead = 0
    behind = 0
    clean = track.strip("[] ")
    for part in clean.split(","):
        part = part.strip()
        if part.startswith("ahead "):
            ahead = int(part.removeprefix("ahead "))
        elif part.startswith("behind "):
            behind = int(part.removeprefix("behind "))
    return ahead, behind


def _timeout_result(
    command: str,
    ctx: ToolContext,
    process_result: dict[str, Any],
    *,
    repo: GitRepo | None = None,
) -> ToolResult:
    error = str(process_result.get("stderr") or "git command timed out").strip()
    data = {
        "stdout": str(process_result.get("stdout") or ""),
        "stderr": error,
        "returncode": int(process_result.get("returncode", -1)),
    }
    payload = {
        "ok": False,
        "command": command,
        "repo_root": repo.repo_root if repo else "",
        "workspace": repo.workspace if repo else str(Path(ctx.workspace).resolve()),
        "data": data,
        "error": error,
    }
    return ToolResult(
        title=f"git: {command}",
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        summary="timed out",
        metadata=tool_timeout_metadata(
            "git",
            command=command,
            returncode=-1,
            error_message=error,
        ),
    )


def _result(
    command: str,
    ctx: ToolContext,
    *,
    repo: GitRepo | None = None,
    ok: bool = True,
    data: dict[str, Any] | None = None,
    error: str = "",
) -> ToolResult:
    payload = {
        "ok": ok,
        "command": command,
        "repo_root": repo.repo_root if repo else "",
        "workspace": repo.workspace if repo else str(Path(ctx.workspace).resolve()),
        "data": data or {},
        "error": error.strip(),
    }
    metadata = {
        "command": command,
        "ok": ok,
    }
    if not ok:
        metadata["error"] = True
        metadata["error_message"] = error.strip()
    return ToolResult(
        title=f"git: {command}",
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        summary="ok" if ok else "failed",
        metadata=metadata,
    )
