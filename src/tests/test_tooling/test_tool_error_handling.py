"""Tests for tool error handling — invalid args, missing error metadata, silent failures."""

import sys
from pathlib import Path


import pytest

from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.builtin.file.read import FileReadTool
from voidx.tooling.builtin.file.manage import ManageTool
from voidx.tooling.builtin.file.replace import FileReplaceTool
from voidx.tooling.builtin.file.search import FindTool, SearchTool
from voidx.tooling.adapters.lsp import LspTool, LspFormatTool
from voidx.tooling.builtin.shell.bash import BashInput
from voidx.tooling.builtin.shell.bash.tool import BashTool
from voidx.agent.adapters.tools.todo import TodoWriteTool
from voidx.agent.adapters.tools.interaction.clarify import ClarifyTool
from voidx.agent.adapters.tools.automation.workflow import WorkflowTool
from voidx.agent.adapters.tools.interaction.checkpoint import PlanCheckpointTool
from voidx.agent.adapters.tools.compaction import CompactContextTool
from voidx.tooling.builtin.web.fetch import WebFetchTool
from voidx.tooling.builtin.web.search import WebSearchTool
from voidx.skills.application.api import SkillsApi
from voidx.skills.registry import SkillRegistry
from voidx.skills.service import SkillService
from voidx.tooling.adapters.skills import SkillsTool
from voidx.tooling.builtin.document import DocumentTool
from voidx.agent.adapters.tools.subagent import AgentTool
from voidx.tooling.builtin.git import GitTool

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
async def test_manage_tool_invalid_args_returns_error():
    result = await ManageTool().execute({"op": "create", "paths": 123}, _CTX)
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
    result = await FindTool().execute({"pattern": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_grep_invalid_args_returns_error():
    result = await SearchTool().execute({"pattern": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_lsp_invalid_args_returns_error():
    result = await LspTool().execute({"operation": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_lsp_format_invalid_args_returns_error():
    result = await LspFormatTool().execute({
        "file_path": "sample.py",
        "start_line": 2,
        "start_character": 0,
        "end_line": 1,
        "end_character": 0,
    }, _CTX)
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
    result = await PlanCheckpointTool().execute({"goal": 123}, _CTX)
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
async def test_skills_invalid_args_returns_error(tmp_path):
    skills_api = SkillsApi(SkillService(SkillRegistry(str(tmp_path))))
    result = await SkillsTool(skills_api).execute({"op": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_document_invalid_args_returns_error():
    result = await DocumentTool().execute({"action": 123}, _CTX)
    assert isinstance(result, ToolResult)
    assert result.metadata.get("error") is True


# ── P1: error metadata must be present on error paths ───────────────────────

@pytest.mark.asyncio
async def test_grep_path_traversal_has_error_metadata():
    """search.py:143 — Path traversal blocked must set error: True."""
    result = await SearchTool().execute(
        {"pattern": "test", "path": "/etc/passwd"}, _CTX
    )
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_grep_invalid_regex_has_error_metadata():
    """search.py:150 — Invalid regex must set error: True."""
    result = await SearchTool().execute(
        {"pattern": "[invalid", "path": "."}, _CTX
    )
    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_grep_path_not_found_has_error_metadata():
    """search.py:216 — Path not found must set error: True."""
    result = await SearchTool().execute(
        {"pattern": "test", "path": "/nonexistent_path_xyz"}, _CTX
    )
    assert result.metadata.get("error") is True




# ── P3: todo error metadata consistency ─────────────────────────────────────

@pytest.mark.asyncio
async def test_todo_update_missing_updates_has_error_true():
    """todo.py:145 — error metadata should use True boolean, not string."""
    result = await TodoWriteTool().execute({"op": "update"}, _CTX)
    assert result.metadata.get("error") is True
    assert "reason" in result.metadata


def test_tool_timeout_metadata_enforces_shared_contract():
    from voidx.tooling.domain.result import tool_timeout_metadata

    metadata = tool_timeout_metadata(
        "shell",
        error=False,
        timeout=False,
        error_kind="forged",
        timeout_source="forged",
        command="sleep 2",
    )

    assert metadata == {
        "command": "sleep 2",
        "error": True,
        "timeout": True,
        "error_kind": "tool_timeout",
        "timeout_source": "shell",
    }
