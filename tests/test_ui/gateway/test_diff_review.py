"""Tests for hunk-level diff review.

The diff review module provides:
- DiffReviewSession: tracks which hunks are approved/rejected
- parse_diff: parse a unified diff into StructuredDiff with reviewable hunks
- apply_review: apply only approved hunks via git apply
- JSON-RPC methods: diff.review, diff.decide, diff.apply
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.ui.gateway.diff_review import DiffReviewSession, HunkDecision


SAMPLE_DIFF = """\
--- a/example.py
+++ b/example.py
@@ -1,3 +1,4 @@
 import os
+import sys
 import json

@@ -10,3 +11,4 @@
 def main():
     pass
+    return None
"""


# ── DiffReviewSession ──────────────────────────────────────────────────


def test_diff_review_session_creates_from_diff():
    session = DiffReviewSession.from_diff(SAMPLE_DIFF)
    assert len(session.files) == 1
    assert session.files[0].path == "example.py"
    assert len(session.files[0].hunks) == 2


def test_diff_review_session_hunks_start_pending():
    session = DiffReviewSession.from_diff(SAMPLE_DIFF)
    for file in session.files:
        for hunk in file.hunks:
            assert hunk.decision == "pending"


def test_diff_review_session_decide_hunk():
    session = DiffReviewSession.from_diff(SAMPLE_DIFF)
    session.decide(file_path="example.py", hunk_index=0, decision="approved")
    assert session.files[0].hunks[0].decision == "approved"
    assert session.files[0].hunks[1].decision == "pending"


def test_diff_review_session_decide_all_hunks():
    session = DiffReviewSession.from_diff(SAMPLE_DIFF)
    session.decide(file_path="example.py", hunk_index=0, decision="approved")
    session.decide(file_path="example.py", hunk_index=1, decision="rejected")
    assert session.files[0].hunks[0].decision == "approved"
    assert session.files[0].hunks[1].decision == "rejected"


def test_diff_review_session_summary():
    session = DiffReviewSession.from_diff(SAMPLE_DIFF)
    session.decide(file_path="example.py", hunk_index=0, decision="approved")
    session.decide(file_path="example.py", hunk_index=1, decision="rejected")
    summary = session.summary()
    assert summary["total_hunks"] == 2
    assert summary["approved"] == 1
    assert summary["rejected"] == 1
    assert summary["pending"] == 0


def test_diff_review_session_approved_diff():
    """approved_diff() returns a unified diff containing only approved hunks."""
    session = DiffReviewSession.from_diff(SAMPLE_DIFF)
    session.decide(file_path="example.py", hunk_index=0, decision="approved")
    session.decide(file_path="example.py", hunk_index=1, decision="rejected")
    approved = session.approved_diff()
    assert "+import sys" in approved
    assert "+    return None" not in approved


def test_diff_review_session_to_snapshot():
    """to_snapshot() returns a JSON-serializable dict for the frontend."""
    session = DiffReviewSession.from_diff(SAMPLE_DIFF)
    snapshot = session.to_snapshot()
    assert "files" in snapshot
    assert len(snapshot["files"]) == 1
    assert snapshot["files"][0]["path"] == "example.py"
    assert len(snapshot["files"][0]["hunks"]) == 2
    assert snapshot["files"][0]["hunks"][0]["decision"] == "pending"


# ── HunkDecision ───────────────────────────────────────────────────────


def test_hunk_decision_values():
    assert HunkDecision.PENDING == "pending"
    assert HunkDecision.APPROVED == "approved"
    assert HunkDecision.REJECTED == "rejected"


# ── Gateway JSON-RPC integration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_diff_review_start_method():
    """diff.review creates a review session and returns snapshot."""
    from voidx.ui.gateway.session import GatewaySession
    from voidx.ui.output.dock import BottomInputDock
    from voidx.ui.protocol.v2.envelope import JsonRpcRequest

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(
        id=1,
        method="diff.review",
        params={"diff": SAMPLE_DIFF},
    )
    result = await session.dispatch_request(request)

    assert result.id == 1
    assert "review_id" in result.result
    assert "snapshot" in result.result
    assert len(result.result["snapshot"]["files"]) == 1


@pytest.mark.asyncio
async def test_gateway_diff_review_decide_method():
    """diff.decide updates a hunk decision."""
    from voidx.ui.gateway.session import GatewaySession
    from voidx.ui.output.dock import BottomInputDock
    from voidx.ui.protocol.v2.envelope import JsonRpcRequest

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    start_req = JsonRpcRequest(
        id=1, method="diff.review", params={"diff": SAMPLE_DIFF},
    )
    start_result = await session.dispatch_request(start_req)
    review_id = start_result.result["review_id"]

    decide_req = JsonRpcRequest(
        id=2,
        method="diff.decide",
        params={
            "review_id": review_id,
            "file_path": "example.py",
            "hunk_index": 0,
            "decision": "approved",
        },
    )
    decide_result = await session.dispatch_request(decide_req)

    assert decide_result.id == 2
    assert decide_result.result["summary"]["approved"] == 1
    assert decide_result.result["summary"]["pending"] == 1


@pytest.mark.asyncio
async def test_gateway_diff_review_apply_method(tmp_path):
    """diff.apply writes approved hunks to the working tree."""
    from voidx.ui.gateway.session import GatewaySession
    from voidx.ui.output.dock import BottomInputDock
    from voidx.ui.protocol.v2.envelope import JsonRpcRequest

    target = tmp_path / "example.py"
    target.write_text("import os\nimport json\n", encoding="utf-8")

    diff_text = (
        f"--- {target}\n"
        f"+++ {target}\n"
        "@@ -1,2 +1,3 @@\n"
        " import os\n"
        "+import sys\n"
        " import json\n"
    )

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    start_req = JsonRpcRequest(
        id=1, method="diff.review", params={"diff": diff_text},
    )
    start_result = await session.dispatch_request(start_req)
    review_id = start_result.result["review_id"]

    decide_req = JsonRpcRequest(
        id=2,
        method="diff.decide",
        params={
            "review_id": review_id,
            "file_path": str(target),
            "hunk_index": 0,
            "decision": "approved",
        },
    )
    await session.dispatch_request(decide_req)

    apply_req = JsonRpcRequest(
        id=3, method="diff.apply", params={"review_id": review_id},
    )
    apply_result = await session.dispatch_request(apply_req)

    assert apply_result.id == 3
    assert str(target) in apply_result.result["files_changed"]
    content = target.read_text(encoding="utf-8")
    assert "import sys" in content
    assert "import os" in content
    assert "import json" in content
