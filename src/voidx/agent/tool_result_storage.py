"""Large tool result persistence — save oversized output to disk for LLM on-demand retrieval."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel

from voidx.memory.store import DATA_DIR

TOOL_RESULT_PERSIST_THRESHOLD = 50_000
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
) -> str:
    if len(content) <= threshold:
        return content

    if tool_name == "read":
        return content

    try:
        file_path = _persist_to_disk(content, tool_use_id, session_id=session_id)
    except OSError:
        return content

    preview = _make_preview(content, preview_chars)

    return (
        f"<persisted-output>\n"
        f"Output too large ({len(content)} chars). Saved to: {file_path}\n"
        f"Preview:\n{preview}\n"
        f"</persisted-output>"
    )


def _persist_to_disk(content: str, tool_use_id: str, *, session_id: str = "default") -> str:
    safe_id = "".join(c for c in tool_use_id if c.isalnum() or c in "-_")
    dir_path = DATA_DIR / "tool-results" / session_id
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


def cleanup_session_results(session_id: str) -> None:
    dir_path = DATA_DIR / "tool-results" / session_id
    if dir_path.exists():
        shutil.rmtree(dir_path, ignore_errors=True)
