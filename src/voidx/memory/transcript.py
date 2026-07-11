"""Persisted UI transcript rows for restoring terminal output trees."""

from __future__ import annotations

import json
import asyncio
from typing import Any

from pydantic import BaseModel, Field

from voidx.memory.jsonl_store import (
    append_session_records,
    read_session_records_from_offset,
    session_dir,
    write_session_json,
)
from voidx.memory.store import _now


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

    await _write_transcript_jsonl_snapshot(session_id, nodes, turn_ids, now)


async def load_transcript(session_id: str) -> list[TranscriptNodeRow]:
    return await _load_transcript_jsonl(session_id) or []


async def clear_transcript(session_id: str) -> None:
    await append_transcript_reset(session_id, reason="clear_transcript")


async def append_transcript_reset(session_id: str, *, reason: str) -> None:
    record = {
        "type": "transcript_reset",
        "reason": reason,
        "created_at": _now(),
    }
    offsets, transcript_size = await append_session_records(session_id, "transcript.jsonl", [record])
    await write_session_json(session_id, "transcript.idx.json", {
        "version": 1,
        "transcript_size": transcript_size,
        "last_reset_offset": offsets[0] if offsets else 0,
        "turn_offsets": {},
        "summary_offsets": {},
        "last_checkpoint_offset": None,
        "last_checkpoint_path": None,
    })


async def append_transcript_summary(session_id: str, *, turn_id: int, content: str) -> None:
    record = {
        "type": "summary",
        "turn_id": turn_id,
        "content": content,
        "created_at": _now(),
    }
    offsets, transcript_size = await append_session_records(session_id, "transcript.jsonl", [record])
    index = _load_transcript_index(session_id) or {
        "version": 1,
        "last_reset_offset": 0,
        "turn_offsets": {},
        "summary_offsets": {},
        "last_checkpoint_offset": None,
        "last_checkpoint_path": None,
    }
    summary_offsets = index.get("summary_offsets")
    if not isinstance(summary_offsets, dict):
        summary_offsets = {}
    summary_offsets[str(turn_id)] = offsets[0] if offsets else 0
    index.update({
        "version": 1,
        "transcript_size": transcript_size,
        "summary_offsets": summary_offsets,
    })
    await write_session_json(session_id, "transcript.idx.json", index)


async def _write_transcript_jsonl_snapshot(
    session_id: str,
    nodes: list[TranscriptNodeRow],
    turn_ids: list[int],
    timestamp: str,
) -> None:
    records = _transcript_snapshot_records(nodes, turn_ids, timestamp)
    offsets, transcript_size = await append_session_records(session_id, "transcript.jsonl", records)
    turn_offsets: dict[str, int] = {}
    last_reset_offset = 0
    summary_offsets: dict[str, int] = {}
    for record, offset in zip(records, offsets, strict=False):
        rtype = record.get("type")
        if rtype == "transcript_reset":
            last_reset_offset = offset
        elif rtype == "turn_start":
            turn_offsets[str(record["turn_id"])] = offset
        elif rtype == "summary":
            summary_offsets[str(record["turn_id"])] = offset
    checkpoint_path = "transcript.checkpoint.json"
    await write_session_json(session_id, checkpoint_path, _checkpoint_payload(nodes, transcript_size))
    await write_session_json(session_id, "transcript.idx.json", {
        "version": 1,
        "transcript_size": transcript_size,
        "last_reset_offset": last_reset_offset,
        "turn_offsets": turn_offsets,
        "summary_offsets": summary_offsets,
        "last_checkpoint_offset": transcript_size,
        "last_checkpoint_path": checkpoint_path,
    })


def _transcript_snapshot_records(
    nodes: list[TranscriptNodeRow],
    turn_ids: list[int],
    timestamp: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [{
        "type": "transcript_reset",
        "reason": "replace_transcript",
        "created_at": timestamp,
    }]
    nodes_by_turn: dict[int, list[TranscriptNodeRow]] = {}
    for node in nodes:
        nodes_by_turn.setdefault(node.turn_id, []).append(node)

    for turn_id in turn_ids:
        records.append({
            "type": "turn_start",
            "turn_id": turn_id,
            "timestamp": timestamp,
        })
        for node in sorted(nodes_by_turn.get(turn_id, []), key=lambda item: (item.sort_order, item.node_id)):
            records.append(_node_record(node))
        records.append({
            "type": "turn_end",
            "turn_id": turn_id,
            "timestamp": timestamp,
        })
    return records


def _node_record(node: TranscriptNodeRow) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "node",
        "turn_id": node.turn_id,
        "node_id": node.node_id,
        "parent_node_id": node.parent_node_id,
        "sort_order": node.sort_order,
        "node_type": node.node_type,
        "header": node.header,
        "body_lines": node.body_lines,
        "status": node.status,
        "collapsed": node.collapsed,
        "created_at": node.created_at,
        "updated_at": node.updated_at,
        "metadata": node.metadata,
    }
    if node.elapsed is not None:
        record["elapsed"] = node.elapsed
    if node.message_id is not None:
        record["message_id"] = node.message_id
    if node.tool_call_id:
        record["tool_call_id"] = node.tool_call_id
    if node.agent_run_id:
        record["agent_run_id"] = node.agent_run_id
    return record


async def _load_transcript_jsonl(session_id: str) -> list[TranscriptNodeRow] | None:
    if not (session_dir(session_id) / "transcript.jsonl").exists():
        return None
    base_rows, records = await _read_transcript_records(session_id)
    if records is None:
        return None

    rows: dict[tuple[int, int], TranscriptNodeRow] = {
        (row.turn_id, row.node_id): row for row in base_rows
    }
    for record in records:
        rtype = record.get("type")
        if rtype == "transcript_reset":
            rows.clear()
            continue
        if rtype == "summary":
            _apply_summary_record(session_id, rows, record)
            continue
        if rtype == "node":
            row = _node_row_from_record(session_id, record)
            if row is not None:
                rows[(row.turn_id, row.node_id)] = row
            continue
        if rtype == "node_update":
            _apply_node_update(rows, record)

    return [
        rows[key]
        for key in sorted(rows, key=lambda item: (item[0], rows[item].sort_order, item[1]))
    ]


async def _read_transcript_records(
    session_id: str,
) -> tuple[list[TranscriptNodeRow], list[dict[str, Any]] | None]:
    path = session_dir(session_id) / "transcript.jsonl"
    index = _load_transcript_index(session_id)
    if (
        index
        and index.get("version") == 1
        and index.get("transcript_size") == path.stat().st_size
        and isinstance(index.get("last_reset_offset"), int)
    ):
        checkpoint_rows = _load_transcript_checkpoint(session_id, index)
        if checkpoint_rows is not None and isinstance(index.get("last_checkpoint_offset"), int):
            return checkpoint_rows, await read_session_records_from_offset(
                session_id,
                "transcript.jsonl",
                int(index["last_checkpoint_offset"]),
            )
        summary_offsets = index.get("summary_offsets")
        if isinstance(summary_offsets, dict):
            latest_summary_offset = max(
                (
                    offset
                    for offset in summary_offsets.values()
                    if isinstance(offset, int) and offset >= int(index["last_reset_offset"])
                ),
                default=None,
            )
            if latest_summary_offset is not None:
                return [], await read_session_records_from_offset(
                    session_id,
                    "transcript.jsonl",
                    latest_summary_offset,
                )
        return [], await read_session_records_from_offset(
            session_id,
            "transcript.jsonl",
            int(index["last_reset_offset"]),
        )
    records = await read_session_records_from_offset(session_id, "transcript.jsonl", 0)
    if records is not None:
        await _rebuild_transcript_index(session_id)
    return [], records


def _load_transcript_index(session_id: str) -> dict[str, Any] | None:
    path = session_dir(session_id) / "transcript.idx.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _checkpoint_payload(nodes: list[TranscriptNodeRow], offset: int) -> dict[str, Any]:
    ordered = sorted(nodes, key=lambda row: (row.turn_id, row.sort_order, row.node_id))
    return {
        "version": 1,
        "offset": offset,
        "rows": [row.model_dump(mode="json") for row in ordered],
    }


def _load_transcript_checkpoint(
    session_id: str,
    index: dict[str, Any],
) -> list[TranscriptNodeRow] | None:
    checkpoint_path = index.get("last_checkpoint_path")
    checkpoint_offset = index.get("last_checkpoint_offset")
    if not isinstance(checkpoint_path, str) or not isinstance(checkpoint_offset, int):
        return None
    path = session_dir(session_id) / checkpoint_path
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("offset") != checkpoint_offset:
        return None
    rows = value.get("rows")
    if not isinstance(rows, list):
        return None
    loaded: list[TranscriptNodeRow] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        try:
            loaded.append(TranscriptNodeRow.model_validate(row))
        except ValueError:
            return None
    return loaded


async def _rebuild_transcript_index(session_id: str) -> None:
    path = session_dir(session_id) / "transcript.jsonl"
    if not path.exists():
        return
    index = await asyncio.to_thread(_scan_transcript_index, path)
    await write_session_json(session_id, "transcript.idx.json", index)


def _scan_transcript_index(path) -> dict[str, Any]:
    last_reset_offset = 0
    turn_offsets: dict[str, int] = {}
    summary_offsets: dict[str, int] = {}
    with path.open("rb") as f:
        while True:
            offset = f.tell()
            raw_line = f.readline()
            if not raw_line:
                break
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                line = raw_line.decode("utf-8")
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            rtype = record.get("type")
            if rtype == "transcript_reset":
                last_reset_offset = offset
                turn_offsets = {}
                summary_offsets = {}
            elif rtype == "turn_start" and isinstance(record.get("turn_id"), int):
                turn_offsets[str(record["turn_id"])] = offset
            elif rtype == "summary" and isinstance(record.get("turn_id"), int):
                summary_offsets[str(record["turn_id"])] = offset
    return {
        "version": 1,
        "transcript_size": path.stat().st_size,
        "last_reset_offset": last_reset_offset,
        "turn_offsets": turn_offsets,
        "summary_offsets": summary_offsets,
        "last_checkpoint_offset": None,
        "last_checkpoint_path": None,
    }


def _apply_summary_record(
    session_id: str,
    rows: dict[tuple[int, int], TranscriptNodeRow],
    record: dict[str, Any],
) -> None:
    turn_id = record.get("turn_id")
    if not isinstance(turn_id, int):
        return
    for key in [key for key in rows if key[0] <= turn_id]:
        rows.pop(key, None)
    content = str(record.get("content") or "")
    created_at = str(record.get("created_at") or _now())
    rows[(turn_id, -1)] = TranscriptNodeRow(
        session_id=session_id,
        turn_id=turn_id,
        node_id=-1,
        sort_order=-1,
        node_type="summary",
        header="Compaction summary",
        body_lines=[content] if content else [],
        status="done",
        created_at=created_at,
        updated_at=created_at,
    )


def _node_row_from_record(session_id: str, record: dict[str, Any]) -> TranscriptNodeRow | None:
    turn_id = record.get("turn_id")
    node_id = record.get("node_id")
    sort_order = record.get("sort_order")
    if not isinstance(turn_id, int) or not isinstance(node_id, int) or not isinstance(sort_order, int):
        return None
    metadata = record.get("metadata")
    body_lines = record.get("body_lines")
    return TranscriptNodeRow(
        session_id=session_id,
        turn_id=turn_id,
        node_id=node_id,
        parent_node_id=record.get("parent_node_id") if isinstance(record.get("parent_node_id"), int) else None,
        sort_order=sort_order,
        node_type=str(record.get("node_type", "")),
        header=str(record.get("header", "")),
        body_lines=[str(item) for item in body_lines] if isinstance(body_lines, list) else [],
        status=str(record.get("status", "running")),
        collapsed=bool(record.get("collapsed", False)),
        elapsed=record.get("elapsed") if isinstance(record.get("elapsed"), int | float) else None,
        message_id=record.get("message_id") if isinstance(record.get("message_id"), int) else None,
        tool_call_id=record.get("tool_call_id") if isinstance(record.get("tool_call_id"), str) else None,
        agent_run_id=record.get("agent_run_id") if isinstance(record.get("agent_run_id"), str) else None,
        metadata=metadata if isinstance(metadata, dict) else {},
        created_at=str(record.get("created_at", "")) or _now(),
        updated_at=str(record.get("updated_at", "")) or _now(),
    )


def _apply_node_update(
    rows: dict[tuple[int, int], TranscriptNodeRow],
    record: dict[str, Any],
) -> None:
    turn_id = record.get("turn_id")
    node_id = record.get("node_id")
    if not isinstance(turn_id, int) or not isinstance(node_id, int):
        return
    row = rows.get((turn_id, node_id))
    if row is None:
        return
    updates: dict[str, Any] = {}
    if "sort_order" in record:
        updates["sort_order"] = record["sort_order"] if isinstance(record["sort_order"], int) else row.sort_order
    if "parent_node_id" in record:
        updates["parent_node_id"] = record["parent_node_id"] if isinstance(record["parent_node_id"], int) else None
    if "header" in record:
        updates["header"] = str(record["header"] or "")
    if "status" in record:
        updates["status"] = str(record["status"] or "running")
    if "collapsed" in record:
        updates["collapsed"] = bool(record["collapsed"])
    if "elapsed" in record:
        updates["elapsed"] = record["elapsed"] if isinstance(record["elapsed"], int | float) else None
    if "body_lines" in record:
        body_lines = record["body_lines"]
        updates["body_lines"] = [str(item) for item in body_lines] if isinstance(body_lines, list) else []
    elif "body_append" in record:
        body_append = record["body_append"]
        if isinstance(body_append, list):
            updates["body_lines"] = [*row.body_lines, *[str(item) for item in body_append]]
    if "message_id" in record:
        updates["message_id"] = record["message_id"] if isinstance(record["message_id"], int) else None
    if "tool_call_id" in record:
        updates["tool_call_id"] = record["tool_call_id"] if isinstance(record["tool_call_id"], str) else None
    if "agent_run_id" in record:
        updates["agent_run_id"] = record["agent_run_id"] if isinstance(record["agent_run_id"], str) else None
    if "updated_at" in record:
        updates["updated_at"] = str(record["updated_at"] or _now())
    if "metadata" in record and isinstance(record["metadata"], dict):
        metadata = dict(row.metadata)
        for key, value in record["metadata"].items():
            if value is None:
                metadata.pop(str(key), None)
            else:
                metadata[str(key)] = value
        updates["metadata"] = metadata
    if "metadata_delete" in record and isinstance(record["metadata_delete"], list):
        metadata = dict(updates.get("metadata", row.metadata))
        for key in record["metadata_delete"]:
            if isinstance(key, str):
                metadata.pop(key, None)
        updates["metadata"] = metadata
    rows[(turn_id, node_id)] = row.model_copy(update=updates)
