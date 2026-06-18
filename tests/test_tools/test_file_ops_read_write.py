"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file_ops import (
    FileReadInput,
    FileWriteInput,
    FileEditInput,
    EditEntry,
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    _find_paragraph,
)
from voidx.tools.file_state import save_file_version
import voidx.tools.file_state as file_state
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput, AgentTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, ClarifyOption, _infer_state_patch
from voidx.tools.load_skills import LoadSkillsTool
from voidx.tools.load_doc_template import LoadDocTemplateTool, LoadDocTemplateInput
from voidx.tools.plan_checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, GoalType, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


def _replace(lineno: int, prefix: str, suffix: str | None = None, new_string: str = "") -> dict:
    return {
        "operation": "replace",
        "lineno": lineno,
        "prefix": prefix,
        "suffix": prefix if suffix is None else suffix,
        "new_string": new_string,
    }


def _insert(lineno: int, prefix: str, suffix: str | None = None, new_string: str = "") -> dict:
    return {
        "operation": "insert",
        "lineno": lineno,
        "prefix": prefix,
        "suffix": prefix if suffix is None else suffix,
        "new_string": new_string,
    }


def _insert_bof(new_string: str) -> dict:
    return {"operation": "insert", "lineno": 0, "prefix": "", "suffix": "", "new_string": new_string}



class TestFileOps:
    """File operations work on real files."""

    def test_write_guidance_is_exposed_to_model(self):
        description = FileWriteTool.description
        schema = FileWriteTool().parameters_schema()
        content_description = schema["properties"]["content"]["description"]

        assert "150 lines" in description
        assert "skeleton" in description
        assert "prefix/suffix" in description
        assert "edit" in description
        assert "read" in description
        assert "150 lines" in content_description
        assert "prefix/suffix" in content_description
        assert "read" in content_description

    @pytest.mark.asyncio
    async def test_read(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "test.txt"}, ctx)
        expected = "1\tline1\n2\tline2\n3\tline3"
        assert result.output.strip() == expected
        assert result.metadata["lines"] == 3
        assert result.metadata["total_lines"] == 3

    @pytest.mark.asyncio
    async def test_read_empty_file_reports_zero_lines(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("read", {"file_path": "empty.txt"}, ctx)

        assert result.metadata["lines"] == 0
        assert result.metadata["total_lines"] == 0
        assert "Read 0 lines" in result.title

    @pytest.mark.asyncio
    async def test_read_rejects_files_with_null_bytes(self, tmp_path):
        f = tmp_path / "binary.dat"
        f.write_bytes(b"text before\0text after\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("read", {"file_path": "binary.dat"}, ctx)

        assert result.metadata.get("error") is True
        assert result.metadata.get("binary") is True
        assert "binary" in result.output.lower()
        assert file_state.covered_read_range(ctx, f, 1, 1) is None

    @pytest.mark.asyncio
    async def test_read_caps_output_by_message_budget_and_records_only_visible_lines(self, tmp_path):
        f = tmp_path / "long-read.txt"
        f.write_text("\n".join(f"line {i:04d} " + ("x" * 80) for i in range(1, 301)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        first = await r.execute_tool("read", {"file_path": "long-read.txt"}, ctx)
        next_offset = first.metadata["next_offset"]

        assert len(first.output) <= DEFAULT_TOOL_MESSAGE_MAX_CHARS
        assert first.metadata["lines"] < 300
        assert first.metadata["truncated_by_chars"] is True
        assert first.metadata["end_line"] == next_offset - 1
        assert file_state.covered_read_range(ctx, f, 1, first.metadata["end_line"]) is not None
        assert file_state.covered_read_range(ctx, f, next_offset, next_offset) is None

        second = await r.execute_tool("read", {"file_path": "long-read.txt", "offset": next_offset, "limit": 5}, ctx)

        assert second.metadata.get("already_read") is not True
        assert f"{next_offset}\tline {next_offset:04d}" in second.output

    @pytest.mark.asyncio
    async def test_read_overlong_single_line_does_not_record_full_line_coverage(self, tmp_path):
        f = tmp_path / "overlong-line.txt"
        f.write_text("prefix-" + ("x" * (DEFAULT_TOOL_MESSAGE_MAX_CHARS + 500)) + "\nsecond\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("read", {"file_path": "overlong-line.txt"}, ctx)

        assert len(result.output) <= DEFAULT_TOOL_MESSAGE_MAX_CHARS
        assert result.metadata["lines"] == 0
        assert result.metadata["truncated_single_line"] is True
        assert file_state.covered_read_range(ctx, f, 1, 1) is None
        assert "not marked as read" in result.output

    def test_record_mtime_uses_ns_and_size_fingerprint(self, tmp_path):
        f = tmp_path / "fingerprint.txt"
        f.write_text("one\n")
        ctx = ToolContext(workspace=str(tmp_path))

        file_state.record_mtime(ctx, f)
        stored = ctx.file_mtimes[str(f.resolve())]

        assert isinstance(stored, dict)
        assert "mtime_ns" in stored
        assert "size" in stored
        assert stored["size"] == 4

    def test_check_staleness_detects_size_change_even_when_mtime_ns_matches(self, tmp_path):
        f = tmp_path / "fingerprint-size.txt"
        f.write_text("one\n")
        ctx = ToolContext(workspace=str(tmp_path))
        file_state.record_mtime(ctx, f)
        f.write_text("one plus more\n")
        key = str(f.resolve())
        current = file_state.file_fingerprint(f)
        ctx.file_mtimes[key] = {"mtime_ns": current.mtime_ns, "size": 4}

        stale = file_state.check_staleness(ctx, f)

        assert stale is not None
        assert "modified since last read" in stale

    @pytest.mark.asyncio
    async def test_read_fully_covered_range_returns_already_read_summary(self, tmp_path):
        f = tmp_path / "covered.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 121)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        first = await r.execute_tool("read", {"file_path": "covered.txt", "offset": 1, "limit": 100}, ctx)

        second = await r.execute_tool("read", {"file_path": "covered.txt", "offset": 50, "limit": 51}, ctx)

        assert "1\tline 1" in first.output
        assert "already read" in second.output.lower()
        assert "50-100" in second.output
        assert "50\tline 50" not in second.output
        assert second.metadata["already_read"] is True
        assert second.metadata["lines"] == 0
        assert second.metadata["covered_lines"] == 51

    @pytest.mark.asyncio
    async def test_write(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("write", {"file_path": "out.txt", "content": "hello"}, ctx)
        assert "File written" in result.output
        assert "Note:" not in result.output
        assert (tmp_path / "out.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_write_warns_after_large_file_is_written(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        content = "\n".join(f"line {i}" for i in range(201))

        result = await r.execute_tool("write", {"file_path": "large.txt", "content": content}, ctx)

        assert "File written: large.txt" in result.output
        assert "This file is large (201 lines)" in result.output
        assert "skeleton" in result.output
        assert "edit" in result.output
        assert (tmp_path / "large.txt").read_text() == content

    @pytest.mark.asyncio
    async def test_write_line_count_matches_read_display_lines(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        exactly_200_with_final_newline = "\n".join(f"line {i}" for i in range(200)) + "\n"

        result = await r.execute_tool(
            "write",
            {"file_path": "exactly-200.txt", "content": exactly_200_with_final_newline},
            ctx,
        )

        assert "Note:" not in result.output
        assert (tmp_path / "exactly-200.txt").read_text() == exactly_200_with_final_newline

    @pytest.mark.asyncio
    async def test_edit(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world\nkeep\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "edit.txt"}, ctx)
        result = await r.execute_tool(
            "edit",
            {"file_path": "edit.txt", "edits": [_replace(1, "hello world", new_string="hi world")]},
            ctx,
        )
        assert "File edited" in result.output
        assert (tmp_path / "edit.txt").read_text() == "hi world\nkeep\n"

    @pytest.mark.asyncio
    async def test_edit_output_contains_diff(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "edit.txt"}, ctx)
        result = await r.execute_tool(
            "edit",
            {"file_path": "edit.txt", "edits": [_replace(1, "hello world", new_string="hi world")]},
            ctx,
        )
        assert "File edited" in result.output
        assert result.diff is not None
        assert "-hello world" in result.diff
        assert "+hi world" in result.diff
        # output should also contain the diff text
        assert "-hello" in result.output or "diff" in result.output.lower()

    @pytest.mark.asyncio
    async def test_edit_line_range_out_of_bounds(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("one\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "short.txt"}, ctx)
        result = await r.execute_tool(
            "edit",
            {"file_path": "short.txt", "edits": [_replace(2, "two", new_string="two")]},
            ctx,
        )
        assert "not found" in result.output
        assert result.metadata.get("error")
        assert (tmp_path / "short.txt").read_text() == "one\n"

    @pytest.mark.asyncio
    async def test_edit_requires_read_coverage_for_replace(self, tmp_path):
        f = tmp_path / "unread.txt"
        f.write_text("one\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "edit",
            {"file_path": "unread.txt", "edits": [_replace(1, "one", new_string="two")]},
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error")
        assert (tmp_path / "unread.txt").read_text() == "one\n"

    @pytest.mark.asyncio
    async def test_edit_insert_line_and_file_start(self, tmp_path):
        f = tmp_path / "insert.txt"
        f.write_text("middle\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert.txt"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "insert.txt",
                "edits": [
                    _insert_bof("top\n"),
                    _insert(2, "end", new_string="bottom\n"),
                ],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert "operations" in result.output
        assert "replacements" not in result.output
        assert result.metadata["operations"] == 2
        assert (tmp_path / "insert.txt").read_text() == "top\nmiddle\nend\nbottom\n"

