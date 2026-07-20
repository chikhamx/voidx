"""Tests for @ attachment and # skill reference candidate RPC methods."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidx.ui.gateway.session import GatewaySession
from voidx.ui.output.dock import BottomInputDock
from voidx.ui.protocol.v2.envelope import JsonRpcRequest, JsonRpcResult

from tests.test_skills.conftest import _write_skill


def _session(workspace: str) -> GatewaySession:
    dock = BottomInputDock()
    return GatewaySession(lambda: dock.tree, thread_id="t1", workspace=workspace)


@pytest.mark.asyncio
async def test_attachments_candidates_lists_workspace_entries(tmp_path: Path):
    (tmp_path / "main.py").write_text("x")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("y")
    session = _session(str(tmp_path))

    result = await session.dispatch_request(
        JsonRpcRequest(id=1, method="attachments.candidates", params={"query": ""})
    )

    assert isinstance(result, JsonRpcResult)
    candidates = result.result["candidates"]
    paths = [c["rel_path"] for c in candidates]
    assert "main.py" in paths
    assert "src/" in paths
    kinds = {c["rel_path"]: c["kind"] for c in candidates}
    assert kinds["src/"] == "dir"
    assert kinds["main.py"] == "file"


@pytest.mark.asyncio
async def test_attachments_candidates_filters_by_query(tmp_path: Path):
    (tmp_path / "main.py").write_text("x")
    (tmp_path / "notes.md").write_text("y")
    session = _session(str(tmp_path))

    result = await session.dispatch_request(
        JsonRpcRequest(id=2, method="attachments.candidates", params={"query": "mai"})
    )

    assert isinstance(result, JsonRpcResult)
    paths = [c["rel_path"] for c in result.result["candidates"]]
    assert paths == ["main.py"]


@pytest.mark.asyncio
async def test_attachments_candidates_uses_thread_workspace(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "other_file.py").write_text("x")
    session = _session(str(tmp_path))
    await session.register_thread("t2", title="t2", workspace=str(other))

    result = await session.dispatch_request(
        JsonRpcRequest(
            id=3,
            method="attachments.candidates",
            params={"thread_id": "t2", "query": ""},
        )
    )

    assert isinstance(result, JsonRpcResult)
    paths = [c["rel_path"] for c in result.result["candidates"]]
    assert "other_file.py" in paths


@pytest.mark.asyncio
async def test_skills_candidates_lists_project_skills(tmp_path: Path):
    _write_skill(
        tmp_path / ".voidx" / "skills",
        "docs",
        "---\nname: docs\ndescription: Write docs\n---\nDocs body",
    )
    session = _session(str(tmp_path))

    result = await session.dispatch_request(
        JsonRpcRequest(id=4, method="skills.candidates", params={"query": ""})
    )

    assert isinstance(result, JsonRpcResult)
    candidates = result.result["candidates"]
    names = [c["name"] for c in candidates]
    assert "docs" in names
    entry = next(c for c in candidates if c["name"] == "docs")
    assert entry["scope"] == "project"
    assert entry["description"] == "Write docs"


@pytest.mark.asyncio
async def test_skills_candidates_filters_by_query(tmp_path: Path):
    _write_skill(
        tmp_path / ".voidx" / "skills",
        "docs",
        "---\nname: docs\ndescription: Write docs\n---\nDocs body",
    )
    _write_skill(
        tmp_path / ".voidx" / "skills",
        "review",
        "---\nname: review\ndescription: Review code\n---\nReview body",
    )
    session = _session(str(tmp_path))

    result = await session.dispatch_request(
        JsonRpcRequest(id=5, method="skills.candidates", params={"query": "doc"})
    )

    assert isinstance(result, JsonRpcResult)
    names = [c["name"] for c in result.result["candidates"]]
    # Project-scope matches rank first; other global skills may also match.
    assert names[0] == "docs"
    assert "review" not in names
