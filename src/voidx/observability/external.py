"""Route selected third-party warnings away from an active terminal UI."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from voidx.observability.tool_log import log_tool_event


class _AgentEventHandler(logging.Handler):
    def __init__(self, *, log_path: Path | None = None) -> None:
        super().__init__(level=logging.WARNING)
        self._log_path = log_path

    def emit(self, record: logging.LogRecord) -> None:
        log_tool_event(
            "python_warning",
            tool_name=record.name,
            message=record.getMessage(),
            log_path=self._log_path,
        )


def install_external_log_bridge(
    logger_name: str,
    *,
    log_path: Path | None = None,
) -> Callable[[], None]:
    logger = logging.getLogger(logger_name)
    handler = _AgentEventHandler(log_path=log_path)
    original_propagate = logger.propagate
    restored = False

    logger.addHandler(handler)
    logger.propagate = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        logger.removeHandler(handler)
        logger.propagate = original_propagate

    return restore
