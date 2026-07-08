"""Tests for tool error handling — invalid args, missing error metadata, silent failures."""

import sys
from pathlib import Path


import pytest

from voidx.tools.base import ToolContext, ToolResult
from voidx.tools.file.read import FileReadTool
from voidx.tools.file.manage import FileTool
from voidx.tools.file.replace import FileReplaceTool
from voidx.tools.search import GlobTool, GrepTool
from voidx.tools.lsp import LspTool, LspFormatTool
from voidx.tools.bash import BashInput
from voidx.tools.bash.tool import BashTool
from voidx.tools.todo import TodoWriteTool
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.clarify import ClarifyTool
from voidx.tools.workflow import WorkflowTool
from voidx.tools.plan_checkpoint import PlanCheckpointTool
from voidx.tools.compact_context import CompactContextTool
from voidx.tools.webfetch import WebFetchTool
from voidx.tools.websearch import WebSearchTool
from voidx.tools.skills import SkillsTool
from voidx.tools.load_doc_template import LoadDocTemplateTool
from voidx.tools.agent import AgentTool
from voidx.tools.git import GitTool

_CTX = ToolContext(workspace="/tmp")


# ── P0: model_validate must be wrapped ──────────────────────────────────────
# Every tool should return a ToolResult with error metadata when given invalid args,
# not raise a raw Pydantic ValidationError.

@pytest.mark.asyncio
async def test_file_read_invalid_args_returns_error():
    result = await FileReadTool().execute({"file_path": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True
    assert "Invalid arguments" in result.output


@pytest.mark.asyncio
async def test_file_tool_invalid_args_returns_error():
    result = await FileTool().execute({"file_path": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_file_replace_invalid_args_returns_error():
    result = await FileReplaceTool().execute({"file_path": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True




@pytest.mark.asyncio
async def test_file_replace_invalid_args_uses_llm_visible_message():
    result = await FileReplaceTool().execute({"file_path": 123}, _CTX)

    assert result.metadata.get("error") is True
    assert "Invalid arguments: field 'bounds' is required" in result.output
    assert "Required fields: file_path, bounds, new_string" in result.output
    assert "FileReplaceInput" not in result.output
    assert "validation errors" not in result.output
@pytest.mark.asyncio
async def test_glob_invalid_args_returns_error():
    result = await GlobTool().execute({"pattern": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_grep_invalid_args_returns_error():
    result = await GrepTool().execute({"pattern": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_lsp_invalid_args_returns_error():
    result = await LspTool().execute({"operation": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_lsp_format_invalid_args_returns_error():
    result = await LspFormatTool().execute({"file_path": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_bash_invalid_args_returns_error():
    result = await BashTool().execute({"command": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_todo_invalid_args_returns_error():
    result = await TodoWriteTool().execute({"op": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_task_status_invalid_args_returns_error():
    result = await TaskStatusTool().execute({"task_id": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_clarify_invalid_args_returns_error():
    result = await ClarifyTool().execute({"question": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_workflow_invalid_args_returns_error():
    result = await WorkflowTool().execute({"action": "enter", "workflow": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_plan_checkpoint_invalid_args_returns_error():
    result = await PlanCheckpointTool().execute({"plan_summary": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_compact_context_invalid_args_returns_error():
    result = await CompactContextTool().execute({"summary": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_webfetch_invalid_args_returns_error():
    result = await WebFetchTool().execute({"url": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_websearch_invalid_args_returns_error():
    result = await WebSearchTool().execute({"query": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_skills_invalid_args_returns_error():
    result = await SkillsTool().execute({"op": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_load_doc_template_invalid_args_returns_error():
    result = await LoadDocTemplateTool().execute({"doc_type": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


# ── P1: error metadata must be present on error paths ───────────────────────

@pytest.mark.asyncio
async def test_grep_path_traversal_has_error_metadata():
    """search.py:143 — Path traversal blocked must set error: True."""
    result = await GrepTool().execute(
        {"pattern": "test", "path": "/etc/passwd"}, _CTX
    )
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_grep_invalid_regex_has_error_metadata():
    """search.py:150 — Invalid regex must set error: True."""
    result = await GrepTool().execute(
        {"pattern": "[invalid", "path": "."}, _CTX
    )
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_grep_path_not_found_has_error_metadata():
    """search.py:216 — Path not found must set error: True."""
    result = await GrepTool().execute(
        {"pattern": "test", "path": "/nonexistent_path_xyz"}, _CTX
    )
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_task_status_not_found_has_error_metadata():
    """task_status.py:41 — Task not found must set error: True."""
    tool = TaskStatusTool()
    tool._tracker = None
    result = await tool.execute({"task_id": "nonexistent"}, _CTX)
    assert result.metadata.get("error") is True


# ── P3: todo error metadata consistency ─────────────────────────────────────

@pytest.mark.asyncio
async def test_todo_update_missing_updates_has_error_true():
    """todo.py:145 — error metadata should use True boolean, not string."""
    result = await TodoWriteTool().execute({"op": "update"}, _CTX)
    assert result.metadata.get("error") is True
    assert "reason" in result.metadata
