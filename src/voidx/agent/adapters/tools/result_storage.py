"""Large tool result persistence — save oversized output to disk for LLM on-demand retrieval."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel

from voidx.agent.application.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
import voidx.persistence.sqlite as store
from voidx.platform.paths import voidx_workspace_dir

TOOL_RESULT_PERSIST_THRESHOLD = DEFAULT_TOOL_MESSAGE_MAX_CHARS
TOOL_RESULT_PREVIEW_CHARS = 2_000
PREVIEW_HEAD_FRACTION = 0.7


class PersistedResult(BaseModel):
    original_size: int
    file_path: str
    preview: str


def maybe_persist_tool_result(
    content: str,
    tool_use_id: str,
    tool_name: str,
    *,
    session_id: str = "default",
    threshold: int = TOOL_RESULT_PERSIST_THRESHOLD,
    preview_chars: int = TOOL_RESULT_PREVIEW_CHARS,
    workspace: str | None = None,
) -> str:
    if len(content) <= threshold:
        return content

    if tool_name == "read":
        return content

    try:
        file_path = _persist_to_disk(content, tool_use_id, session_id=session_id, workspace=workspace)
    except OSError:
        return content

    preview = _make_preview(content, preview_chars)

    return (
        f"<persisted-output>\n"
        f"Output too large ({len(content)} chars). Saved to: {file_path}\n"
        f"Preview:\n{preview}\n"
        f"</persisted-output>"
    )


def _tool_results_root(workspace: str | None = None) -> Path:
    if workspace:
        try:
            return voidx_workspace_dir(workspace) / "tool-results"
        except OSError:
            pass
    return store.DATA_DIR / "tool-results"


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


def _make_preview(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    head_n = int(limit * PREVIEW_HEAD_FRACTION)
    tail_n = limit - head_n
    return content[:head_n] + "\n…\n" + content[-tail_n:]


def cleanup_session_results(session_id: str, workspace: str | None = None) -> None:
    roots = [store.DATA_DIR / "tool-results"]
    if workspace:
        workspace_root = _tool_results_root(workspace)
        if workspace_root not in roots:
            roots.insert(0, workspace_root)

    for root in roots:
        dir_path = root / session_id
        if dir_path.exists():
            shutil.rmtree(dir_path, ignore_errors=True)
