"""Persisted UI transcript rows for restoring terminal output trees."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from voidx.memory.store import _fetch_all, _write_transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TranscriptTurnRow(BaseModel):
    session_id: str
    turn_id: int
    user_message_id: int | None = None
    created_at: str = Field(default_factory=_now)
    completed_at: str | None = None


class TranscriptNodeRow(BaseModel):
    session_id: str
    turn_id: int
    node_id: int
    parent_node_id: int | None = None
    sort_order: int
    node_type: str
    header: str = ""
    body_lines: list[str] = Field(default_factory=list)
    status: str = "running"
    collapsed: bool = False
    elapsed: float | None = None
    message_id: int | None = None
    tool_call_id: str | None = None
    agent_run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


async def replace_transcript(
    session_id: str,
    nodes: list[TranscriptNodeRow],
    *,
    turn_count: int | None = None,
) -> None:
    """Replace a session transcript snapshot atomically."""
    now = _now()
    if turn_count is None:
        turn_ids = sorted({node.turn_id for node in nodes})
    else:
        turn_ids = list(range(turn_count))

    def _run(conn):
        conn.execute("DELETE FROM transcript_nodes WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
        for turn_id in turn_ids:
            conn.execute(
                """INSERT INTO turns (session_id, turn_id, created_at, completed_at)
                   VALUES (?, ?, ?, ?)""",
                (session_id, turn_id, now, now),
            )
        for node in nodes:
            conn.execute(
                """INSERT INTO transcript_nodes (
                       session_id, turn_id, node_id, parent_node_id, sort_order,
                       node_type, header, body_json, status, collapsed, elapsed,
                       message_id, tool_call_id, agent_run_id, metadata_json,
                       created_at, updated_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    node.turn_id,
                    node.node_id,
                    node.parent_node_id,
                    node.sort_order,
                    node.node_type,
                    node.header,
                    json.dumps(node.body_lines, ensure_ascii=False),
                    node.status,
                    1 if node.collapsed else 0,
                    node.elapsed,
                    node.message_id,
                    node.tool_call_id,
                    node.agent_run_id,
                    json.dumps(node.metadata, ensure_ascii=False),
                    node.created_at,
                    node.updated_at,
                ),
            )

    await _write_transaction(_run)


async def load_transcript(session_id: str) -> list[TranscriptNodeRow]:
    rows = await _fetch_all(
        """SELECT * FROM transcript_nodes
           WHERE session_id = ?
           ORDER BY turn_id ASC, sort_order ASC, node_id ASC""",
        (session_id,),
    )
    return [
        TranscriptNodeRow(
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            node_id=row["node_id"],
            parent_node_id=row["parent_node_id"],
            sort_order=row["sort_order"],
            node_type=row["node_type"],
            header=row["header"],
            body_lines=json.loads(row["body_json"] or "[]"),
            status=row["status"],
            collapsed=bool(row["collapsed"]),
            elapsed=row["elapsed"],
            message_id=row["message_id"],
            tool_call_id=row["tool_call_id"],
            agent_run_id=row["agent_run_id"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


async def clear_transcript(session_id: str) -> None:
    def _run(conn):
        conn.execute("DELETE FROM transcript_nodes WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))

    await _write_transaction(_run)
