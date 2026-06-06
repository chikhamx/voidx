"""Dock status records and status-bar helpers."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.ui.output.dock.formatting import _clean
from voidx.ui.output.dock.state import get_dock
from voidx.ui.output.tree import OutputNode


@dataclass(frozen=True)
class DockStatusRecord:
    status_id: str
    label: str
    detail: str = ""
    stage: str = "working"


def active_agent_step_text() -> str:
    current = get_dock()
    status_record = getattr(current, "status_record", None)
    if not callable(status_record):
        return ""
    record = status_record("agent:-1:progress")
    if record is None:
        return ""
    return _agent_step_text(record.label)


def _agent_step_text(label: str) -> str:
    text = _clean(label).strip()
    prefix = "Agent step "
    if text.startswith(prefix):
        return "step " + text[len(prefix):]
    return text



class DockStatusMixin:
    def record_status(
        self,
        status_id: str,
        label: str,
        detail: str = "",
        *,
        stage: str = "working",
    ) -> DockStatusRecord:
        record = DockStatusRecord(
            status_id=status_id,
            label=label,
            detail=detail,
            stage=stage,
        )
        self._status_records[status_id] = record
        self.refresh()
        return record

    def clear_status_record(self, status_id: str) -> None:
        if status_id in self._status_records:
            self._status_records.pop(status_id, None)
            self.refresh()

    def status_record(self, status_id: str) -> DockStatusRecord | None:
        return self._status_records.get(status_id)

