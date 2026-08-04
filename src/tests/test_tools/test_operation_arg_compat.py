from __future__ import annotations

import pytest

from voidx.runtime.task_state import ToolStatePatch
from voidx.tools.base import ToolContext
from voidx.tools.agent import AgentTool
from voidx.tools.document import DocumentTool
from voidx.tools.lsp import LspTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.skills import SkillsTool
from voidx.tools.todo import TodoWriteTool
from voidx.tools.workflow import WorkflowTool
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus


class EmptyTodoTracker:
    def get_todos(self):
        return []


@pytest.mark.asyncio
async def test_agent_spawn_accepts_current_schema(tmp_path) -> None:
    result = await AgentTool().execute(
        {
            "mode": "review",
            "goal": "Inspect the target and report concrete findings.",
            "detail": "Return structured findings.",
            "scope": "src/voidx/tools/agent.py",
        },
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("validation_error") is not True
    assert result.metadata.get("reason") == "no_resolver"


@pytest.mark.asyncio
async def test_agent_rejects_unknown_control_fields(tmp_path) -> None:
    result = await AgentTool().execute(
        {"action": "wait", "run_id": "run_123", "wait": "brief"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("reason") == "gateway_unavailable"
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_todo_read_ignores_write_update_noise(tmp_path) -> None:
    result = await TodoWriteTool(EmptyTodoTracker()).execute(
        {
            "op": "read",
            "filter": "null",
            "todos": "not a todo list",
            "updates": "not an update list",
        },
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("error") is not True
    assert "Invalid arguments" not in result.output


@pytest.mark.asyncio
async def test_workflow_done_ignores_enter_and_advance_noise(tmp_path) -> None:
    ctx = ToolContext(
        workspace=str(tmp_path),
        workflow_runs=[WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)],
    )

    result = await WorkflowTool().execute(
        {
            "action": "done",
            "workflow": "null",
            "condition": {"ignored": True},
            "goal": "x" * 500,
        },
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert ToolStatePatch.model_validate(result.metadata["state_patch"])


@pytest.mark.asyncio
async def test_document_list_ignores_null_path(tmp_path) -> None:
    result = await DocumentTool().execute(
        {"action": "list", "path": "null"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("error") is not True
    assert "Invalid arguments" not in result.output


@pytest.mark.asyncio
async def test_lsp_diagnostics_ignores_position_noise(tmp_path) -> None:
    result = await LspTool().execute(
        {
            "operation": "diagnostics",
            "line": "null",
            "character": "null",
            "include_declaration": "not-a-bool",
        },
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("error") is True
    assert result.output == "LSP manager not available."


@pytest.mark.asyncio
async def test_skill_list_ignores_load_create_noise(tmp_path) -> None:
    result = await SkillsTool().execute(
        {
            "op": "list",
            "name": {"ignored": True},
            "description": ["ignored"],
            "body": {"ignored": True},
            "scope": "null",
        },
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("error") is not True
    assert "Invalid arguments" not in result.output


@pytest.mark.asyncio
async def test_file_manage_delete_ignores_create_overwrite_noise(tmp_path) -> None:
    target = tmp_path / "delete.txt"
    target.write_text("remove me", encoding="utf-8")

    result = await ToolRegistry().execute_tool(
        "manage",
        {
            "op": "delete",
            "paths": "delete.txt",
            "overwrite": {"ignored": True},
        },
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("error") is not True
    assert not target.exists()


@pytest.mark.asyncio
async def test_file_manage_create_ignores_move_noise(tmp_path) -> None:
    result = await ToolRegistry().execute_tool(
        "manage",
        {
            "op": "create",
            "kind": "null",
            "paths": "created.txt",
            "moves": "not move specs",
        },
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("error") is not True
    assert (tmp_path / "created.txt").exists()


@pytest.mark.asyncio
async def test_file_write_append_ignores_insert_lineno_noise(tmp_path) -> None:
    target = tmp_path / "append.txt"
    target.write_text("a\n", encoding="utf-8")
    registry = ToolRegistry()
    ctx = ToolContext(workspace=str(tmp_path))
    await registry.execute_tool("read", {"file_path": "append.txt"}, ctx)

    result = await registry.execute_tool(
        "write",
        {
            "op": "append",
            "file_path": "append.txt",
            "lineno": "null",
            "new_string": "b\n",
        },
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert target.read_text(encoding="utf-8") == "a\nb\n"
