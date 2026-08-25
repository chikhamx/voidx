"""Shared persisted session models and profile snapshot codecs."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from voidx.agent.domain.agent_profile import PROFILE_NAME_RE, AgentProfileSnapshot
from voidx.llm.domain.model import DEFAULT_MODEL
from voidx.persistence.sqlite import now


class SessionInfo(BaseModel):
    id: str
    title: str = "New session"
    workspace: str = "."
    directory: str = ""
    model_provider: str = "anthropic"
    model_name: str = DEFAULT_MODEL
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    message_count: int = 0
    runtime_profile: str = "coding"
    profile_snapshot: AgentProfileSnapshot | None = None


def validate_runtime_profile(profile: str) -> str:
    normalized = profile.strip()
    if not PROFILE_NAME_RE.match(normalized):
        raise ValueError(f"unknown runtime profile: {profile}")
    return normalized


def snapshot_columns(snapshot: AgentProfileSnapshot | None) -> tuple:
    if snapshot is None:
        return (None, None, None, None, None)
    return (
        snapshot.revision,
        snapshot.content_hash,
        snapshot.snapshot_hash,
        snapshot.source,
        json.dumps(snapshot.canonical_payload, sort_keys=True, ensure_ascii=False),
    )


def snapshot_from_row(row) -> AgentProfileSnapshot | None:
    payload_json = row["runtime_profile_snapshot"]
    if not payload_json:
        return None
    return AgentProfileSnapshot(
        profile_id=row["runtime_profile"],
        revision=row["runtime_profile_revision"] or 1,
        source=row["runtime_profile_source"] or "bundled",
        content_hash=row["runtime_profile_content_hash"] or "",
        snapshot_hash=row["runtime_profile_hash"] or "",
        canonical_payload=json.loads(payload_json),
    )


__all__ = [
    "SessionInfo",
    "snapshot_columns",
    "snapshot_from_row",
    "validate_runtime_profile",
]
