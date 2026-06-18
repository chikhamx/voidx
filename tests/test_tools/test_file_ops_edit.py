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
    @pytest.mark.asyncio
    async def test_single_read_allows_one_batch_edit_with_multiple_covered_ranges(self, tmp_path):
        f = tmp_path / "batch.txt"
        f.write_text("one\ntwo\nthree\nfour\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "batch.txt"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "batch.txt",
                "edits": [
                    _replace(1, "one", new_string="ONE"),
                    _replace(3, "three", "four", "THREE\nFOUR\n"),
                ],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "batch.txt").read_text() == "ONE\ntwo\nTHREE\nFOUR\n"

    @pytest.mark.asyncio
    async def test_insert_tool_uses_line_number_and_content_only(self, tmp_path):
        f = tmp_path / "insert-tool.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert-tool.txt", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "insert",
            {"file_path": "insert-tool.txt", "lineno": 1, "new_string": "middle\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "insert-tool.txt").read_text() == "one\nmiddle\ntwo\n"

    @pytest.mark.asyncio
    async def test_insert_tool_requires_read_coverage_for_target_line(self, tmp_path):
        f = tmp_path / "insert-unread.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "insert",
            {"file_path": "insert-unread.txt", "lineno": 1, "new_string": "middle\n"},
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error")
        assert f.read_text() == "one\ntwo\n"

    @pytest.mark.asyncio
    async def test_insert_tool_allows_beginning_of_file_without_prior_read(self, tmp_path):
        f = tmp_path / "insert-bof.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "insert",
            {"file_path": "insert-bof.txt", "lineno": 0, "new_string": "zero\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "zero\none\ntwo\n"

    @pytest.mark.asyncio
    async def test_replace_tool_replaces_exact_text_segment_near_lineno(self, tmp_path):
        f = tmp_path / "replace-tool.txt"
        f.write_text("one\ntwo = 2\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "replace-tool.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "replace-tool.txt",
                "lineno": 2,
                "prefix": "two",
                "suffix": "two",
                "new_string": "TWO",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\nTWO = 2\nthree\n"

    @pytest.mark.asyncio
    async def test_edit_rejects_overlapping_ranges(self, tmp_path):
        f = tmp_path / "overlap.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "overlap.txt"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "overlap.txt",
                "edits": [
                    _replace(1, "one", "two", "x"),
                    _replace(2, "two", "three", "y"),
                ],
            },
            ctx,
        )

        assert "overlap" in result.output.lower()
        assert result.metadata.get("error")
        assert (tmp_path / "overlap.txt").read_text() == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    async def test_edit_preserves_read_coverage_after_success(self, tmp_path):
        f = tmp_path / "coverage.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "coverage.txt"}, ctx)
        first = await r.execute_tool(
            "edit",
            {"file_path": "coverage.txt", "edits": [_replace(1, "one", new_string="ONE")]},
            ctx,
        )

        second = await r.execute_tool(
            "edit",
            {"file_path": "coverage.txt", "edits": [_replace(2, "two", new_string="TWO")]},
            ctx,
        )

        assert first.metadata.get("error") is not True
        assert second.metadata.get("error") is not True
        assert (tmp_path / "coverage.txt").read_text() == "ONE\nTWO"

    @pytest.mark.asyncio
    async def test_edit_does_not_mark_unseen_lines_as_read_after_partial_edit(self, tmp_path):
        f = tmp_path / "partial-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 13)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "partial-coverage.txt", "offset": 1, "limit": 2}, ctx)

        edit = await r.execute_tool(
            "edit",
            {
                "file_path": "partial-coverage.txt",
                "edits": [_replace(2, "line 2", new_string="LINE 2")],
            },
            ctx,
        )
        reread = await r.execute_tool("read", {"file_path": "partial-coverage.txt", "offset": 10, "limit": 1}, ctx)

        assert edit.metadata.get("error") is not True
        assert reread.metadata.get("already_read") is not True
        assert "10\tline 10" in reread.output

    @pytest.mark.asyncio
    async def test_edit_expand_remaps_read_coverage_precisely(self, tmp_path):
        f = tmp_path / "expand-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 41)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "expand-coverage.txt", "offset": 1, "limit": 30}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "expand-coverage.txt",
                "edits": [_replace(5, "line 5", new_string="line 5a\nline 5b")],
            },
            ctx,
        )
        reread = await r.execute_tool("read", {"file_path": "expand-coverage.txt", "offset": 32, "limit": 1}, ctx)

        assert result.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 31) is not None
        assert reread.metadata.get("already_read") is not True
        assert "32\tline 31" in reread.output

    @pytest.mark.asyncio
    async def test_edit_delete_remaps_read_coverage_precisely(self, tmp_path):
        f = tmp_path / "delete-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete-coverage.txt"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "delete-coverage.txt",
                "edits": [_replace(50, "line 50", new_string="")],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 99) is not None

    @pytest.mark.asyncio
    async def test_edit_read_same_line_after_diff_is_already_read(self, tmp_path):
        f = tmp_path / "same-line.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "same-line.txt", "offset": 1, "limit": 2}, ctx)

        edit = await r.execute_tool(
            "edit",
            {"file_path": "same-line.txt", "edits": [_replace(2, "two", new_string="TWO")]},
            ctx,
        )
        reread = await r.execute_tool("read", {"file_path": "same-line.txt", "offset": 2, "limit": 1}, ctx)

        assert edit.metadata.get("error") is not True
        assert reread.metadata.get("already_read")

    @pytest.mark.asyncio
    async def test_edit_multi_hunk_remaps_and_merges_coverage(self, tmp_path):
        f = tmp_path / "multi-hunk-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-hunk-coverage.txt"}, ctx)

        result = await r.execute_tool(
            "edit",
            {
                "file_path": "multi-hunk-coverage.txt",
                "edits": [
                    _replace(10, "line 10", new_string="line 10a\nline 10b"),
                    _replace(50, "line 50", new_string=""),
                ],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 100) is not None

    @pytest.mark.asyncio
    async def test_edit_noop_refreshes_read_coverage_fingerprint(self, tmp_path):
        f = tmp_path / "noop-coverage.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "noop-coverage.txt", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "edit",
            {"file_path": "noop-coverage.txt", "edits": [_replace(1, "one", new_string="one")]},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 1) is not None

    @pytest.mark.asyncio
    async def test_edit_after_partial_edit_still_rejects_unread_target(self, tmp_path):
        f = tmp_path / "partial-edit.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 13)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "partial-edit.txt", "offset": 1, "limit": 2}, ctx)

        first = await r.execute_tool(
            "edit",
            {
                "file_path": "partial-edit.txt",
                "edits": [_replace(2, "line 2", new_string="LINE 2")],
            },
            ctx,
        )
        second = await r.execute_tool(
            "edit",
            {
                "file_path": "partial-edit.txt",
                "edits": [_replace(10, "line 10", new_string="LINE 10")],
            },
            ctx,
        )

        assert first.metadata.get("error") is not True
        assert "read" in second.output.lower()
        assert second.metadata.get("error")
        assert (tmp_path / "partial-edit.txt").read_text().splitlines()[9] == "line 10"

    @pytest.mark.asyncio
    async def test_merge_overlapping_read_ranges(self, tmp_path):
        f = tmp_path / "merge.txt"
        f.write_text("\n".join(str(i) for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "merge.txt", "offset": 1, "limit": 50}, ctx)
        await r.execute_tool("read", {"file_path": "merge.txt", "offset": 40, "limit": 61}, ctx)

        result = await r.execute_tool(
            "edit",
            {"file_path": "merge.txt", "edits": [_replace(1, "1", new_string="CHANGED")]},
            ctx,
        )
        assert result.metadata.get("error") is not True

    @pytest.mark.asyncio
    async def test_merge_adjacent_read_ranges(self, tmp_path):
        f = tmp_path / "adjacent.txt"
        f.write_text("\n".join(str(i) for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "adjacent.txt", "offset": 1, "limit": 50}, ctx)
        await r.execute_tool("read", {"file_path": "adjacent.txt", "offset": 51, "limit": 50}, ctx)

        result = await r.execute_tool(
            "edit",
            {"file_path": "adjacent.txt", "edits": [_replace(50, "50", "51", "MERGED")]},
            ctx,
        )
        assert result.metadata.get("error") is not True

    @pytest.mark.asyncio
    async def test_non_adjacent_ranges_not_covered(self, tmp_path):
        f = tmp_path / "gap.txt"
        f.write_text("\n".join(str(i) for i in range(1, 31)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "gap.txt", "offset": 1, "limit": 10}, ctx)
        await r.execute_tool("read", {"file_path": "gap.txt", "offset": 20, "limit": 11}, ctx)

        result = await r.execute_tool(
            "edit",
            {"file_path": "gap.txt", "edits": [_replace(15, "15", new_string="GAP")]},
            ctx,
        )
        assert "read" in result.output.lower()
        assert result.metadata.get("error")

    @pytest.mark.asyncio
    async def test_insert_lineno_out_of_range_gives_friendly_error(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "insert",
            {"file_path": "short.txt", "lineno": 5, "new_string": "oops\n"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "insert" in result.output.lower() or "line" in result.output.lower()
        assert "Edit 0" not in result.output

    @pytest.mark.asyncio
    async def test_insert_into_empty_file_at_lineno_0(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "insert",
            {"file_path": "empty.txt", "lineno": 0, "new_string": "first\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "first\n"

    @pytest.mark.asyncio
    async def test_insert_at_end_of_file(self, tmp_path):
        f = tmp_path / "end.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "end.txt"}, ctx)

        result = await r.execute_tool(
            "insert",
            {"file_path": "end.txt", "lineno": 2, "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo\nthree\n"


    @pytest.mark.asyncio
    async def test_insert_at_end_of_file_with_lineno_minus_one(self, tmp_path):
        f = tmp_path / "end.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "end.txt"}, ctx)

        result = await r.execute_tool(
            "insert",
            {"file_path": "end.txt", "lineno": -1, "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    async def test_insert_into_empty_file_with_lineno_minus_one(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "insert",
            {"file_path": "empty.txt", "lineno": -1, "new_string": "first\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "first\n"
    @pytest.mark.asyncio
    async def test_insert_remaps_read_coverage(self, tmp_path):
        f = tmp_path / "remap.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "remap.txt"}, ctx)

        result = await r.execute_tool(
            "insert",
            {"file_path": "remap.txt", "lineno": 3, "new_string": "inserted\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        # After inserting 1 line at line 3, original line 10 shifts to line 11.
        # Read coverage is remapped so lines 1-11 are covered.
        reread = await r.execute_tool("read", {"file_path": "remap.txt", "offset": 11, "limit": 1}, ctx)
        assert reread.metadata.get("already_read")

    @pytest.mark.asyncio
    async def test_replace_deletes_text_segment_with_empty_new_string(self, tmp_path):
        f = tmp_path / "delete.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete.txt", "lineno": 2, "prefix": "two\n", "suffix": "two\n", "new_string": ""},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\nthree\n"

    @pytest.mark.asyncio
    async def test_replace_multiline_text_segment(self, tmp_path):
        f = tmp_path / "multi.txt"
        f.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "multi.txt", "lineno": 1, "prefix": "def foo():", "suffix": "pass", "new_string": "def foo():\n    return 42"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "def foo():\n    return 42\n\ndef bar():\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_text_outside_30_line_window(self, tmp_path):
        f = tmp_path / "window.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 80)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "window.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "window.txt", "lineno": 1, "prefix": "line 40", "suffix": "line 40", "new_string": "LINE 40"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "30" in result.output
        assert "line 40" in f.read_text()

    @pytest.mark.asyncio
    async def test_replace_rejects_nonexistent_prefix(self, tmp_path):
        f = tmp_path / "nope.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "nope.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "nope.txt", "lineno": 1, "prefix": "nonexistent", "suffix": "nonexistent", "new_string": "X"},
            ctx,
        )

        assert result.metadata.get("error")
        assert f.read_text() == "one\ntwo\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_ambiguous_text_segment_in_window(self, tmp_path):
        f = tmp_path / "ambiguous.txt"
        f.write_text("target\nmiddle\ntarget\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "ambiguous.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "ambiguous.txt", "lineno": 2, "prefix": "target", "suffix": "target", "new_string": "TARGET"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "ambiguous" in result.output.lower()
        assert f.read_text() == "target\nmiddle\ntarget\n"
