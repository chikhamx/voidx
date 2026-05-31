"""Context-local dock proxy."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_current_dock: ContextVar[Any | None] = ContextVar("current_dock", default=None)


def get_dock() -> Any | None:
    return _current_dock.get()


def set_dock(dock: Any | None) -> None:
    _current_dock.set(dock)


class _DockProxy:
    @property
    def active(self) -> bool:
        d = get_dock()
        return d.active if d is not None else False

    def __getattr__(self, name):
        d = get_dock()
        if d is None:
            if name in ("active",):
                return False
            if name in (
                "begin_capture",
                "deactivate",
                "print",
                "capture",
                "start_turn",
                "set_stream",
                "start_tool",
                "tool_output",
                "append_tool_result",
                "refresh",
                "set_mode",
            ):
                return lambda *args, **kwargs: None
            raise RuntimeError(f"No active dock in this context. Cannot access '{name}'.")
        return getattr(d, name)

    def __bool__(self):
        return get_dock() is not None


dock = _DockProxy()
