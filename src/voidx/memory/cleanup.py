"""Session cleanup planning utilities."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from voidx.memory.jsonl_store import session_dir
from voidx.memory.session import delete_session, list_sessions


class SessionDeleteCandidate(BaseModel):
    session_id: str
    title: str
    workspace: str
    updated_at: str
    message_count: int
    file_bytes_to_reclaim: int = 0
    bytes_to_reclaim: int = 0


class SessionDeletePlan(BaseModel):
    scope: str
    dry_run: bool = True
    candidates: list[SessionDeleteCandidate] = Field(default_factory=list)

    @property
    def total_sessions(self) -> int:
        return len(self.candidates)

    @property
    def empty_sessions(self) -> int:
        return sum(1 for candidate in self.candidates if candidate.message_count == 0)

    @property
    def sessions_with_messages(self) -> int:
        return sum(1 for candidate in self.candidates if candidate.message_count > 0)

    @property
    def bytes_to_reclaim(self) -> int:
        return sum(candidate.bytes_to_reclaim for candidate in self.candidates)


async def plan_session_delete(
    scope: str = "30d",
    *,
    now: str | None = None,
) -> SessionDeletePlan:
    normalized_scope = _normalize_scope(scope)
    cutoff = _cutoff_for_scope(normalized_scope, now=now)
    sessions = await list_sessions(limit=10000)
    candidates: list[SessionDeleteCandidate] = []
    for session in sessions:
        if cutoff is not None and _parse_datetime(session.updated_at) >= cutoff:
            continue
        file_bytes = await _session_disk_usage(session_dir(session.id))
        candidates.append(SessionDeleteCandidate(
            session_id=session.id,
            title=session.title,
            workspace=session.workspace,
            updated_at=session.updated_at,
            message_count=session.message_count,
            file_bytes_to_reclaim=file_bytes,
            bytes_to_reclaim=file_bytes,
        ))
    candidates.sort(key=lambda item: (item.updated_at, item.session_id))
    return SessionDeletePlan(scope=normalized_scope, candidates=candidates)


async def apply_session_delete_plan(plan: SessionDeletePlan) -> int:
    deleted = 0
    for candidate in plan.candidates:
        await delete_session(candidate.session_id)
        deleted += 1
    return deleted


def _normalize_scope(scope: str) -> str:
    value = (scope or "30d").strip().lower()
    if value in {"all", "*"}:
        return "all"
    if value.endswith("days"):
        value = value[:-4].strip() + "d"
    if value.endswith("day"):
        value = value[:-3].strip() + "d"
    if value.endswith("d") and value[:-1].isdigit() and int(value[:-1]) >= 0:
        return value
    raise ValueError("Usage: /session del [--dry-run] [7d|15d|30d|all]")


def _cutoff_for_scope(scope: str, *, now: str | None) -> datetime | None:
    if scope == "all":
        return None
    days = int(scope[:-1])
    return _parse_datetime(now) - timedelta(days=days)


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


async def _session_disk_usage(path: Path) -> int:
    if not path.exists():
        return 0
    return await asyncio.to_thread(_directory_size, path)


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total

