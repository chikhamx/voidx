"""Serialize and restore OutputTree snapshots for session resume."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal, cast

from voidx.memory.service import TranscriptNodeRow
from voidx.ui.output.tree import OutputNode, OutputTree

NodeType = Literal[
    "root",
    "startup",
    "turn",
    "tool_call",
    "tool_result",
    "todo",
    "subagent",
    "message",
    "assistant",
    "thought",
    "status",
    "permission",
    "checkpoint",
    "error",
    "warn",
    "diff",
]
Status = Literal["running", "done", "error"]
_NODE_TYPES = set(NodeType.__args__)
_STATUSES = set(Status.__args__)


def tree_to_transcript_rows(session_id: str, tree: OutputTree) -> tuple[list[TranscriptNodeRow], int]:
    rows: list[TranscriptNodeRow] = []
    turn_id = -1
    next_node_id = 0
    sort_order = 0

    def add_node(node: OutputNode, parent_node_id: int | None) -> None:
        nonlocal next_node_id, sort_order
        if _is_blank_separator(node):
            return
        node_id = next_node_id
        next_node_id += 1
        metadata = {
            "tree_id": node.id,
            "header_style": node.header_style,
            "agent_name": node.agent_name,
            "step_info": node.step_info,
            "meta": node.meta,
            "payload": node.payload,
        }
        rows.append(
            TranscriptNodeRow(
                session_id=session_id,
                turn_id=turn_id,
                node_id=node_id,
                parent_node_id=parent_node_id,
                sort_order=sort_order,
                node_type=node.node_type,
                header=node.header,
                body_lines=list(node.body_lines),
                status=node.status,
                collapsed=node.collapsed,
                elapsed=node.elapsed,
                message_id=node.message_id,
                tool_call_id=node.tool_call_id,
                agent_run_id=node.agent_run_id,
                metadata={key: value for key, value in metadata.items() if value not in (None, "", {})},
            )
        )
        sort_order += 1
        for child in node.children:
            add_node(child, node_id)

    for child in tree.root.children:
        if child.node_type == "startup":
            continue
        if _is_blank_separator(child):
            continue
        if child.node_type == "turn":
            turn_id += 1
            next_node_id = 0
            sort_order = 0
        elif turn_id < 0:
            continue
        add_node(child, None)

    return rows, max(turn_id + 1, 0)


def transcript_rows_to_tree(rows: list[TranscriptNodeRow]) -> OutputTree:
    tree = OutputTree()
    by_turn: dict[int, list[TranscriptNodeRow]] = defaultdict(list)
    for row in rows:
        by_turn[row.turn_id].append(row)

    for turn_id in sorted(by_turn):
        nodes: dict[int, OutputNode] = {}
        pending = sorted(by_turn[turn_id], key=lambda row: (row.sort_order, row.node_id))
        for row in pending:
            metadata = row.metadata
            payload = metadata.get("payload")
            node = OutputNode(
                id=f"t{turn_id}:n{row.node_id}",
                node_type=_node_type(row.node_type),
                header=row.header,
                header_style=str(metadata.get("header_style") or ""),
                body_lines=list(row.body_lines),
                collapsed=row.collapsed,
                status=_status(row.status),
                elapsed=row.elapsed,
                agent_name=_optional_str(metadata.get("agent_name")),
                step_info=_optional_str(metadata.get("step_info")),
                meta=_optional_str(metadata.get("meta")),
                tool_call_id=row.tool_call_id,
                agent_run_id=row.agent_run_id,
                message_id=row.message_id,
                payload=payload if isinstance(payload, dict) else {},
            )
            nodes[row.node_id] = node
            parent = tree.root if row.parent_node_id is None else nodes.get(row.parent_node_id)
            if parent is not None:
                tree.add_node(parent, node)

    tree.mark_dirty()
    return tree


def _is_blank_separator(node: OutputNode) -> bool:
    return node.node_type == "message" and not node.header and not node.body_lines and not node.children


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _node_type(value: str) -> NodeType:
    if value in _NODE_TYPES:
        return cast(NodeType, value)
    return "message"


def _status(value: str) -> Status:
    if value in _STATUSES:
        return cast(Status, value)
    return "running"
