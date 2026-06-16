"""Agent event logger for hidden tool failures and UI warnings/errors.

Writes structured JSONL entries to ``~/.voidx/logs/agent_events.jsonl``
so that errors suppressed from the conversation can still be inspected later.

Rotates when the log file exceeds 5 MB, keeping up to 3 rotated copies
(``agent_events.1.jsonl``, ``agent_events.2.jsonl``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = Path.home() / ".voidx" / "logs"
_LOG_FILE_NAME = "agent_events.jsonl"
_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB
_MAX_LOG_FILES = 3


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < _MAX_LOG_BYTES:
            return
    except OSError:
        return
    for i in range(_MAX_LOG_FILES - 1, 0, -1):
        older = path.parent / f"{path.stem}.{i}{path.suffix}"
        newer = path.parent / f"{path.stem}.{i - 1}{path.suffix}" if i > 1 else path
        if newer.exists():
            try:
                newer.replace(older)
            except OSError:
                pass
    try:
        path.write_text("", encoding="utf-8")
    except OSError:
        pass


def log_tool_event(
    event: str,
    *,
    tool_name: str = "",
    message: str = "",
    session_id: str | None = None,
    log_dir: Path = _DEFAULT_LOG_DIR,
    log_path: Path | None = None,
) -> None:
    try:
        target = log_path if log_path is not None else log_dir / _LOG_FILE_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)

        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        if tool_name:
            entry["tool_name"] = tool_name
        if message:
            entry["message"] = message
        if session_id is not None:
            entry["session_id"] = session_id

        line = json.dumps(entry, ensure_ascii=False, default=str)
        with target.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("Failed to write agent event log", exc_info=True)
