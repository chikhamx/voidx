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
from voidx.tools.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
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
    @pytest.mark.asyncio
    async def test_read_coverage_uses_mtime_ns_and_size_fingerprint(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "fp.txt"
        f.write_text("hello\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "fp.txt"}, ctx)

        key = str((tmp_path / "fp.txt").resolve())
        coverage = ctx.file_read_coverage[key]
        fp = fs.file_fingerprint(tmp_path / "fp.txt")
        assert coverage["fingerprint"] == {"mtime_ns": fp.mtime_ns, "size": fp.size}

        result = await r.execute_tool(
            "edit",
            {"file_path": "fp.txt", "edits": [_replace(1, "hello", new_string="HELLO")]},
            ctx,
        )
        assert result.metadata.get("error") is not True

    @pytest.mark.asyncio
    async def test_edit_prefix_suffix_handles_shifted_line_before_editing(self, tmp_path):
        f = tmp_path / "paragraph.py"
        f.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "def bar():\n"
            "    value = 2\n"
            "    return value\n"
        )
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph.py"}, ctx)

        shifted = await r.execute_tool(
            "edit",
            {"file_path": "paragraph.py", "edits": [_insert(2, "return 1", new_string="    extra = 0\n")]},
            ctx,
        )
        corrected = await r.execute_tool(
            "edit",
            {
                "file_path": "paragraph.py",
                "edits": [_replace(4, "def bar():", new_string="def baz():")],
            },
            ctx,
        )

        assert shifted.metadata.get("error") is not True
        assert corrected.metadata.get("error") is not True
        assert "def baz():\n    value = 2" in (tmp_path / "paragraph.py").read_text()

    @pytest.mark.asyncio
    async def test_edit_prefix_reports_ambiguous_and_missing_matches(self, tmp_path):
        f = tmp_path / "paragraph-errors.py"
        f.write_text("target = 1\nother = 0\ntarget = 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-errors.py"}, ctx)

        ambiguous = await r.execute_tool(
            "edit",
            {
                "file_path": "paragraph-errors.py",
                "edits": [_replace(2, "target", new_string="changed")],
            },
            ctx,
        )
        missing = await r.execute_tool(
            "edit",
            {
                "file_path": "paragraph-errors.py",
                "edits": [_replace(2, "missing", new_string="changed")],
            },
            ctx,
        )

        assert "ambiguous" in ambiguous.output.lower()
        assert ambiguous.metadata.get("error")
        assert "not found" in missing.output.lower()
        assert missing.metadata.get("error")
        assert (tmp_path / "paragraph-errors.py").read_text() == "target = 1\nother = 0\ntarget = 2\n"

    @pytest.mark.asyncio
    async def test_edit_lineno_hint_disambiguates_nearest_prefix(self, tmp_path):
        f = tmp_path / "nearest.py"
        f.write_text("def item():\n    a = 1\n\ndef item():\n    a = 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "nearest.py"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "nearest.py",
                "edits": [_replace(4, "def item():", "a = 2", "def item():\n    a = 3")],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "nearest.py").read_text() == "def item():\n    a = 1\n\ndef item():\n    a = 3"

    @pytest.mark.asyncio
    async def test_edit_multiline_prefix_replaces_multiline_range(self, tmp_path):
        f = tmp_path / "multi-line-prefix.py"
        f.write_text("top\ninserted\nstart\nmiddle\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-line-prefix.py"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "multi-line-prefix.py",
                "edits": [_replace(2, "inserted\nstart", "middle", "START\nMIDDLE")],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "multi-line-prefix.py").read_text() == "top\nSTART\nMIDDLE\nend\n"

    @pytest.mark.asyncio
    async def test_edit_insert_uses_prefix_suffix_after_shifted_line(self, tmp_path):
        f = tmp_path / "insert-paragraph-correct.py"
        f.write_text("top\ninserted\ntarget\nbottom\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert-paragraph-correct.py"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "insert-paragraph-correct.py",
                "edits": [_insert(2, "target", new_string="after target\n")],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "insert-paragraph-correct.py").read_text() == "top\ninserted\ntarget\nafter target\nbottom\n"

    @pytest.mark.asyncio
    async def test_edit_paragraph_resolution_still_requires_read_coverage(self, tmp_path):
        f = tmp_path / "paragraph-coverage.py"
        f.write_text("top\nmiddle\ntarget\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-coverage.py", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "paragraph-coverage.py",
                "edits": [_replace(1, "target", new_string="TARGET")],
            },
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error")
        assert (tmp_path / "paragraph-coverage.py").read_text() == "top\nmiddle\ntarget\n"

    @pytest.mark.asyncio
    async def test_edit_paragraph_resolution_revalidates_batch_conflicts(self, tmp_path):
        f = tmp_path / "paragraph-conflict.py"
        f.write_text("top\ntarget\nbottom\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-conflict.py"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "paragraph-conflict.py",
                "edits": [
                    _replace(2, "target", new_string="TARGET"),
                    _replace(1, "target", new_string="TARGET 2"),
                ],
            },
            ctx,
        )

        assert "overlap" in result.output.lower()
        assert result.metadata.get("error")
        assert (tmp_path / "paragraph-conflict.py").read_text() == "top\ntarget\nbottom\n"

    @pytest.mark.asyncio
    async def test_edit_reports_line_shift_for_insert_and_delete(self, tmp_path):
        f = tmp_path / "shift.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "shift.txt"}, ctx)

        inserted = await r.execute_tool(
            "edit",
            {"file_path": "shift.txt", "edits": [_insert_bof("zero\n")]},
            ctx,
        )
        deleted = await r.execute_tool(
            "edit",
            {"file_path": "shift.txt", "edits": [_replace(2, "two", new_string="")]},
            ctx,
        )

        assert inserted.metadata.get("error") is not True
        assert "Line shift: all existing lines shifted by +1" in inserted.output
        assert deleted.metadata.get("error") is not True
        assert "Line shift: lines after 3 shifted by -1" in deleted.output

    @pytest.mark.asyncio
    async def test_edit_same_line_count_replace_does_not_report_shift(self, tmp_path):
        f = tmp_path / "no-shift.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "no-shift.txt"}, ctx)

        result = await r.execute_tool(
            "edit",
            {"file_path": "no-shift.txt", "edits": [_replace(2, "two", new_string="TWO")]},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert "Line shift:" not in result.output

    @pytest.mark.asyncio
    async def test_edit_reports_multiple_line_shift_hints(self, tmp_path):
        f = tmp_path / "multi-shift.txt"
        f.write_text("one\ntwo\nthree\nfour\nfive\nsix\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-shift.txt"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "multi-shift.txt",
                "edits": [
                    _insert(1, "one", new_string="one-a\n"),
                    _replace(5, "five", new_string=""),
                ],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert "Line shift: lines after 1 shifted by +1" in result.output
        assert "Line shift: lines after 5 shifted by -1" in result.output

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "nope.txt"}, ctx)
        assert "File not found" in result.output

    @pytest.mark.asyncio
    async def test_read_offset_beyond_file(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("line1\nline2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "short.txt", "offset": 100}, ctx)
        assert result.metadata["lines"] == 0
        assert "beyond" in result.output.lower() or "offset" in result.output.lower()


