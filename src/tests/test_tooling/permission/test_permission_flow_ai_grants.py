"""Tests for AI approval writing runtime grants for external paths."""

from __future__ import annotations

from tests.langgraph_execution import make_langgraph_execution
from pathlib import Path
from types import SimpleNamespace

import pytest


def _graph(workspace):
    from voidx.config import Config
    from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
    cfg = Config(workspace=str(workspace))
    return make_langgraph_execution(cfg, api_key="test")


@pytest.mark.asyncio
async def test_ai_approval_writes_runtime_grant_for_external_path(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"

    graph = _graph(workspace)
    graph._permission.permission_mode = "ai_approval"
    graph._settings = SimpleNamespace()
    graph._ai_approval = SimpleNamespace()

    async def fake_review(candidates, settings):
        allowed_ids = frozenset(
            d.tool_call.get("id") for d in candidates
        )
        return SimpleNamespace(
            allowed_ids=allowed_ids,
            reason="reviewed",
            skipped_reasons={},
            denied_reasons={},
            reviewed_ids=allowed_ids,
        )

    graph._ai_approval.review = fake_review

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
    )

    assert len(approved) == 1
    assert denied == []
    grants = tuple(g for g in graph._permission.grant_snapshot() if g.persistence == "runtime")
    assert any(g.object_type == "file" and str(target) in g.path for g in grants)
