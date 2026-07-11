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


PERMISSION_REQUEST_STATUS_ID = "permission:request"


def active_permission_request_text() -> str:
    record = _status_record(PERMISSION_REQUEST_STATUS_ID)
    if record is None:
        return ""
    return _clean(record.label).strip()


def active_permission_request_detail_text() -> str:
    record = _status_record(PERMISSION_REQUEST_STATUS_ID)
    if record is None:
        return ""
    return _clean(record.detail).strip()


def active_agent_step_text() -> str:
    record = _status_record("agent:-1:progress")
    if record is None:
        return ""
    return _agent_step_text(record.label)


def active_guidance_preview_text() -> str:
    current = get_dock()
    if current is None:
        return ""
    return getattr(current, "_guidance_preview", "") or ""


def active_turn_analyzing_text() -> str:
    record = _status_record("turn:analyzing")
    if record is None:
        return ""
    return _clean(record.label).strip()


def active_compaction_text() -> str:
    record = _status_record("compaction")
    if record is None:
        return ""
    return _clean(record.label).strip()


def active_compaction_detail_text() -> str:
    record = _status_record("compaction")
    if record is None:
        return ""
    return _clean(record.detail).strip()


def active_llm_retry_text() -> str:
    record = _status_record("llm:retry")
    if record is None:
        return ""
    return _clean(record.label).strip()


def active_llm_retry_detail_text() -> str:
    record = _status_record("llm:retry")
    if record is None:
        return ""
    return _clean(record.detail).strip()


def active_error_text() -> str:
    record = _status_record("error:current")
    if record is None:
        return ""
    return _clean(record.label).strip()


def active_error_detail_text() -> str:
    record = _status_record("error:current")
    if record is None:
        return ""
    return _clean(record.detail).strip()


def _status_record(status_id: str) -> DockStatusRecord | None:
    current = get_dock()
    status_record = getattr(current, "status_record", None)
    if not callable(status_record):
        return None
    return status_record(status_id)


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
