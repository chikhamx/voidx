"""Tests for protocol v2 snapshot models (multi-session)."""

from __future__ import annotations

from voidx.presentation.protocol.transcript import TranscriptNode
from voidx.presentation.protocol.v2.snapshot import ThreadSnapshot, WorkspaceSnapshot
from voidx.presentation.protocol.v2.threads import ThreadInfo


def _node(node_id: str) -> TranscriptNode:
    return TranscriptNode(id=node_id, node_type="message")


def test_thread_snapshot_defaults():
    snap = ThreadSnapshot(thread_id="t1")
    assert snap.thread_id == "t1"
    assert snap.revision == 0
    assert snap.nodes == []


def test_thread_snapshot_with_nodes():
    snap = ThreadSnapshot(
        thread_id="t1",
        revision=5,
        nodes=[_node("n1"), _node("n2")],
    )
    assert snap.revision == 5
    assert len(snap.nodes) == 2
    assert snap.nodes[0].id == "n1"


def test_workspace_snapshot_defaults():
    snap = WorkspaceSnapshot()
    assert snap.threads == []
    assert snap.active_thread_id == ""
    assert snap.active_snapshot is None
    assert snap.provider == ""
    assert snap.model == ""
    assert snap.workspace == ""
    assert snap.profile_configured is None


def test_workspace_snapshot_with_threads():
    t1 = ThreadInfo(thread_id="t1", title="Session 1")
    t2 = ThreadInfo(thread_id="t2", title="Session 2")
    active = ThreadSnapshot(thread_id="t1", revision=3, nodes=[_node("n1")])
    snap = WorkspaceSnapshot(
        threads=[t1, t2],
        active_thread_id="t1",
        active_snapshot=active,
    )
    assert len(snap.threads) == 2
    assert snap.active_thread_id == "t1"
    assert snap.active_snapshot is not None
    assert snap.active_snapshot.thread_id == "t1"
    assert len(snap.active_snapshot.nodes) == 1


def test_workspace_snapshot_can_carry_runtime_status():
    snap = WorkspaceSnapshot(
        provider="openai",
        model="gpt-5.5",
        workspace="/Users/chikham/workspace/voidx",
        profile_configured=True,
    )

    assert snap.provider == "openai"
    assert snap.model == "gpt-5.5"
    assert snap.workspace.endswith("/voidx")
    assert snap.profile_configured is True


def test_workspace_snapshot_non_active_threads_have_no_snapshot():
    """Only the active thread carries a full transcript snapshot."""
    t1 = ThreadInfo(thread_id="t1")
    t2 = ThreadInfo(thread_id="t2")
    snap = WorkspaceSnapshot(
        threads=[t1, t2],
        active_thread_id="t1",
        active_snapshot=ThreadSnapshot(thread_id="t1"),
    )
    # t2 is listed as a thread but has no snapshot — frontend must call
    # session.switch to fetch it on demand.
    assert snap.active_snapshot.thread_id == "t1"
