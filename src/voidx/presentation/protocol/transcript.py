"""Transcript snapshot DTOs for transport-oriented UI frontends."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from voidx.presentation.protocol.node_types import NodeType, Status
from voidx.presentation.output.tree import OutputNode, OutputTree


class TranscriptNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    parent_id: str | None = None
    node_type: NodeType
    status: Status = "running"
    title: str = ""
    header: str = ""
    header_style: str = ""
    body_lines: list[str] = Field(default_factory=list)
    collapsed: bool = False
    elapsed: float | None = None
    agent_name: str | None = None
    step_info: str | None = None
    meta: str | None = None
    tool_call_id: str | None = None
    agent_run_id: str | None = None
    message_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    child_ids: list[str] = Field(default_factory=list)


class TranscriptSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str = ""
    revision: int = 0
    root_id: str = "root"
    nodes: list[TranscriptNode] = Field(default_factory=list)


def tree_to_snapshot(
    tree: OutputTree,
    *,
    session_id: str = "",
    revision: int = 0,
) -> TranscriptSnapshot:
    nodes: list[TranscriptNode] = []

    def visit(node: OutputNode, parent_id: str | None) -> None:
        if node is not tree.root:
            nodes.append(_node_to_snapshot(node, parent_id))
        for child in node.children:
            visit(child, None if node is tree.root else node.id)

    visit(tree.root, None)
    return TranscriptSnapshot(session_id=session_id, revision=revision, nodes=nodes)


def _node_to_snapshot(node: OutputNode, parent_id: str | None) -> TranscriptNode:
    return TranscriptNode(
        id=node.id,
        parent_id=parent_id,
        node_type=node.node_type,
        status=node.status,
        title=node.header,
        header=node.header,
        header_style=node.header_style,
        body_lines=list(node.body_lines),
        collapsed=node.collapsed,
        elapsed=node.elapsed,
        agent_name=node.agent_name,
        step_info=node.step_info,
        meta=node.meta,
        tool_call_id=node.tool_call_id,
        agent_run_id=node.agent_run_id,
        message_id=node.message_id,
        payload=dict(node.payload),
        child_ids=[child.id for child in node.children],
    )
