from __future__ import annotations

from pathlib import Path

from voidx.platform.paths import voidx_home, voidx_workspace_dir

def persist_named_tool_result(
    content: str,
    name: str,
    *,
    session_id: str = "default",
    workspace: str | None = None,
) -> str:
    return _persist_to_disk(content, name, session_id=session_id, workspace=workspace)


def _persist_to_disk(
    content: str,
    tool_use_id: str,
    *,
    session_id: str = "default",
    workspace: str | None = None,
) -> str:
    safe_id = "".join(c for c in tool_use_id if c.isalnum() or c in "-_")
    dir_path = _tool_results_root(workspace) / session_id
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / f"{safe_id}.txt"
    file_path.write_text(content, encoding="utf-8", errors="replace")
    return str(file_path)


def _tool_results_root(workspace: str | None = None) -> Path:
    if workspace:
        try:
            return voidx_workspace_dir(workspace) / "tool-results"
        except OSError:
            pass
    return voidx_home() / "tool-results"
