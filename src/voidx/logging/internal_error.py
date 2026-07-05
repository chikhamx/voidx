"""Internal error logger — captures suppressed exceptions to JSONL.

Writes structured JSONL entries to ``~/.voidx/logs/internal_error.jsonl``
so that errors swallowed by ``except Exception: pass`` handlers and
stderr ``print()`` calls can still be inspected later.

Rotates when the log file exceeds 5 MB, keeping up to 3 rotated copies
(``internal_error.1.jsonl``, ``internal_error.2.jsonl``).
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from voidx.paths import voidx_logs_dir

_DEFAULT_LOG_DIR = voidx_logs_dir()
_LOG_FILE_NAME = "internal_error.jsonl"
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


def log_internal_error(
    exc: BaseException,
    *,
    context: str,
    session_id: str | None = None,
    log_dir: Path = _DEFAULT_LOG_DIR,
    log_path: Path | None = None,
) -> None:
    try:
        target = log_path if log_path is not None else log_dir / _LOG_FILE_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)

        entry: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "internal_error",
            "context": context,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "traceback": "".join(traceback.format_exception(exc)),
        }
        if session_id is not None:
            entry["session_id"] = session_id

        line = json.dumps(entry, ensure_ascii=False, default=str)
        with target.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("Failed to write internal error log", exc_info=True)
