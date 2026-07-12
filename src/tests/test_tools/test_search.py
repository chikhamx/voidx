"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import shlex
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file import FileReadInput, FileReadTool
from voidx.tools.file.state import save_file_version
import voidx.tools.file.state as file_state
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput, AgentTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
from voidx.tools.skills import SkillsTool
from voidx.tools.document import DocumentTool, DocumentInput
from voidx.tools.checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


class TestSearch:
    """Search tools find files deterministically."""

    @pytest.mark.asyncio
    async def test_glob(self, tmp_path):
        (tmp_path / "a.py").touch()
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").touch()
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("glob", {"pattern": "**/*.py"}, ctx)
        data = json.loads(result.output)
        assert data["matches"] == 2
        assert "a.py" in data["files"]
        assert "sub/b.py" in [f.replace("\\", "/") for f in data["files"]]
        assert "a.py" in result.display

    @pytest.mark.asyncio
    async def test_glob_ignore_case(self, tmp_path):
        (tmp_path / "App.PY").touch()
        (tmp_path / "notes.txt").touch()
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("glob", {"pattern": "**/*.py", "ignore_case": True}, ctx)
        data = json.loads(result.output)
        assert "App.PY" in data["files"]
        assert "notes.txt" not in data["files"]

    @pytest.mark.asyncio
    async def test_glob_max_depth(self, tmp_path):
        (tmp_path / "a.py").touch()
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").touch()
        (tmp_path / "sub" / "nested").mkdir()
        (tmp_path / "sub" / "nested" / "c.py").touch()
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("glob", {"pattern": "**/*.py", "max_depth": 2}, ctx)
        data = json.loads(result.output)
        files_normalized = [f.replace("\\", "/") for f in data["files"]]
        assert "a.py" in data["files"]
        assert "sub/b.py" in files_normalized
        assert "sub/nested/c.py" not in files_normalized

    @pytest.mark.asyncio
    async def test_grep(self, tmp_path):
        (tmp_path / "code.py").write_text("TODO: fix this\nprint('ok')\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        data = json.loads(result.output)
        assert data["matches"] >= 1
        assert any(r["file"] == "code.py" for r in data["results"])
        assert "code.py" in result.display
        assert "TODO" in result.display

    @pytest.mark.asyncio
    async def test_grep_no_match(self, tmp_path):
        (tmp_path / "code.py").write_text("nothing here\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "XYZNOTFOUND"}, ctx)
        data = json.loads(result.output)
        assert data["matches"] == 0
        assert "No matches" in result.display

    @pytest.mark.asyncio
    async def test_grep_handles_unreadable_file_and_continues(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.py"
        good = tmp_path / "good.py"
        bad.write_text("TODO hidden\n")
        good.write_text("TODO visible\n")
        original_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == bad:
                raise OSError("cannot read")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("grep", {"pattern": "TODO", "include": "*.py"}, ctx)

        assert "good.py" in result.display
        assert "TODO visible" in result.display

    @pytest.mark.asyncio
    async def test_grep_sandbox_readable_dir_returns_absolute_file(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        target = external / "outside.txt"
        target.write_text("OUTSIDE_MARKER\n", encoding="utf-8")

        ctx = ToolContext(workspace=str(workspace), sandbox_readable_dirs=[str(external)])
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "OUTSIDE_MARKER", "path": str(external)}, ctx)
        data = json.loads(result.output)

        assert data["matches"] == 1
        assert data["results"][0]["file"] == str(target)
        assert data["results"][0]["line"] == 1
        assert data["results"][0]["content"] == "OUTSIDE_MARKER"
        assert str(target) in result.display


class TestGrepImprovements:
    """P0 grep improvements: ignore_case, whole_word, context_lines, exclude."""

    @pytest.mark.asyncio
    async def test_ignore_case(self, tmp_path):
        (tmp_path / "code.py").write_text("class Foo:\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "foo", "ignore_case": True}, ctx)
        assert "Foo" in result.display
        assert result.metadata["matches"] == 1

    @pytest.mark.asyncio
    async def test_ignore_case_off_by_default(self, tmp_path):
        (tmp_path / "code.py").write_text("class Foo:\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "foo"}, ctx)
        assert "No matches" in result.display

    @pytest.mark.asyncio
    async def test_whole_word(self, tmp_path):
        (tmp_path / "code.py").write_text("def func():\n    function_call()\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "func", "whole_word": True}, ctx)
        assert "def func():" in result.display
        assert "function_call" not in result.display

    @pytest.mark.asyncio
    async def test_whole_word_off_by_default(self, tmp_path):
        (tmp_path / "code.py").write_text("def func():\n    function_call()\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "func"}, ctx)
        assert "func" in result.display
        assert "function_call" in result.display

    @pytest.mark.asyncio
    async def test_context_lines(self, tmp_path):
        (tmp_path / "code.py").write_text("line1\nline2\nTARGET\nline4\nline5\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TARGET", "context_lines": 1}, ctx)
        assert "code.py:3:TARGET" in result.display
        assert "code.py-2-line2" in result.display
        assert "code.py-4-line4" in result.display

    @pytest.mark.asyncio
    async def test_context_lines_zero_no_context(self, tmp_path):
        (tmp_path / "code.py").write_text("line1\nTARGET\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TARGET", "context_lines": 0}, ctx)
        assert "code.py:2:TARGET" in result.display
        assert "code.py-" not in result.display

    @pytest.mark.asyncio
    async def test_context_lines_clamped_at_file_boundaries(self, tmp_path):
        (tmp_path / "code.py").write_text("FIRST\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "FIRST", "context_lines": 2}, ctx)
        assert "code.py:1:FIRST" in result.display
        assert "code.py-2-line2" in result.display
        assert "code.py-3-line3" in result.display
        # No line 0 or negative
        lines = result.display.strip().split("\n")
        assert all(not "-0-" in l and not "--" in l.split(":")[0] if "-" in l.split(":")[0] else True for l in lines)

    @pytest.mark.asyncio
    async def test_exclude(self, tmp_path):
        (tmp_path / "app.py").write_text("TODO in app\n")
        (tmp_path / "app.min.js").write_text("TODO in min\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO", "exclude": "*.min.js"}, ctx)
        assert "app.py" in result.display
        assert "app.min.js" not in result.display

    @pytest.mark.asyncio
    async def test_exclude_none_includes_all(self, tmp_path):
        (tmp_path / "a.py").write_text("TODO a\n")
        (tmp_path / "b.js").write_text("TODO b\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert "a.py" in result.display
        assert "b.js" in result.display

    @pytest.mark.asyncio
    async def test_ignore_case_with_whole_word(self, tmp_path):
        (tmp_path / "code.py").write_text("class Foo:\n    foo = 1\n    foobar = 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "foo", "ignore_case": True, "whole_word": True}, ctx)
        assert result.metadata["matches"] == 2
        assert "Foo" in result.display
        assert "foo" in result.display
        assert "foobar" not in result.display

    @pytest.mark.asyncio
    async def test_context_lines_deduplicates_adjacent_context(self, tmp_path):
        (tmp_path / "code.py").write_text("line1\nMATCH_A\nline3\nMATCH_B\nline5\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "MATCH_", "context_lines": 1}, ctx)
        assert "code.py:2:MATCH_A" in result.display
        assert "code.py:4:MATCH_B" in result.display
        assert "code.py-3-line3" in result.display
        assert result.display.count("code.py-3-line3") == 1

    @pytest.mark.asyncio
    async def test_metadata_truncated_on_match_limit(self, tmp_path):
        for i in range(110):
            (tmp_path / f"f{i}.py").write_text("TODO\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert result.metadata["truncated"] is True

    @pytest.mark.asyncio
    async def test_metadata_truncated_false_when_complete(self, tmp_path):
        (tmp_path / "code.py").write_text("TODO\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert result.metadata["truncated"] is False

    @pytest.mark.asyncio
    async def test_context_lines_negative_rejected(self, tmp_path):
        (tmp_path / "code.py").write_text("TODO\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO", "context_lines": -1}, ctx)
        assert result.metadata.get("error") is True
        assert "Invalid arguments" in result.output

    @pytest.mark.asyncio
    async def test_exclude_multiple_globs(self, tmp_path):
        (tmp_path / "app.py").write_text("TODO in app\n")
        (tmp_path / "app.min.js").write_text("TODO in min\n")
        (tmp_path / "app.map").write_text("TODO in map\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO", "exclude": ["*.min.js", "*.map"]}, ctx)
        assert "app.py" in result.display
        assert "app.min.js" not in result.display
        assert "app.map" not in result.display

    @pytest.mark.asyncio
    async def test_exclude_single_string_still_works(self, tmp_path):
        (tmp_path / "app.py").write_text("TODO in app\n")
        (tmp_path / "app.min.js").write_text("TODO in min\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO", "exclude": "*.min.js"}, ctx)
        assert "app.py" in result.display
        assert "app.min.js" not in result.display


class TestGrepGitignore:
    """P1: .gitignore-aware file filtering."""

    @pytest.mark.asyncio
    async def test_gitignore_excludes_ignored_files(self, tmp_path):
        (tmp_path / ".gitignore").write_text("build/\n*.log\n")
        (tmp_path / "app.py").write_text("TODO in app\n")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "output.py").write_text("TODO in build\n")
        (tmp_path / "debug.log").write_text("TODO in log\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert "app.py" in result.display
        assert "build/output.py" not in result.display
        assert "debug.log" not in result.display

    @pytest.mark.asyncio
    async def test_gitignore_absent_falls_back_to_skip_dirs(self, tmp_path):
        (tmp_path / "app.py").write_text("TODO in app\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert "app.py" in result.display

    @pytest.mark.asyncio
    async def test_gitignore_negation_pattern(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n!important.log\n")
        (tmp_path / "debug.log").write_text("TODO debug\n")
        (tmp_path / "important.log").write_text("TODO important\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert "important.log" in result.display
        assert "debug.log" not in result.display


class TestGrepStructuredResults:
    """P1: Structured match details in metadata."""

    @pytest.mark.asyncio
    async def test_match_details_contains_file_line_column_content(self, tmp_path):
        (tmp_path / "code.py").write_text("def hello():\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "hello"}, ctx)
        details = result.metadata.get("match_details", [])
        assert len(details) == 1
        assert details[0]["file"] == "code.py"
        assert details[0]["line"] == 1
        assert details[0]["column"] == 5
        assert "hello" in details[0]["content"]

    @pytest.mark.asyncio
    async def test_match_details_multiple_matches(self, tmp_path):
        (tmp_path / "a.py").write_text("TODO first\n")
        (tmp_path / "b.py").write_text("TODO second\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        details = result.metadata.get("match_details", [])
        assert len(details) == 2
        files = {d["file"] for d in details}
        assert "a.py" in files
        assert "b.py" in files

    @pytest.mark.asyncio
    async def test_match_details_empty_on_no_match(self, tmp_path):
        (tmp_path / "code.py").write_text("nothing here\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "MISSING"}, ctx)
        assert result.metadata.get("match_details", []) == []


class TestGrepBinaryDetection:
    """P1: Binary file content detection instead of suffix-only filtering."""

    @pytest.mark.asyncio
    async def test_binary_file_skipped_by_content(self, tmp_path):
        (tmp_path / "data.dat").write_bytes(b"TODO\x00binary\n")
        (tmp_path / "app.py").write_text("TODO in app\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert "app.py" in result.display
        assert "data.dat" not in result.display

    @pytest.mark.asyncio
    async def test_text_file_with_unusual_suffix_still_searched(self, tmp_path):
        (tmp_path / "config.xyz").write_text("TODO in config\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert "config.xyz" in result.display


class TestGrepConfigurableLimits:
    """P1: Configurable max_matches and max_scanned."""

    @pytest.mark.asyncio
    async def test_max_matches_limits_results(self, tmp_path):
        for i in range(20):
            (tmp_path / f"f{i}.py").write_text("TODO\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO", "max_matches": 5}, ctx)
        assert result.metadata["matches"] == 5
        assert result.metadata["truncated"] is True

    @pytest.mark.asyncio
    async def test_max_scanned_limits_file_scan(self, tmp_path):
        for i in range(30):
            (tmp_path / f"f{i}.py").write_text("TODO\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO", "max_scanned": 10}, ctx)
        assert result.metadata["matches"] <= 10
        assert result.metadata["truncated"] is True

    @pytest.mark.asyncio
    async def test_default_limits_unchanged(self, tmp_path):
        (tmp_path / "code.py").write_text("TODO\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert result.metadata["matches"] == 1
        assert result.metadata["truncated"] is False


class TestGrepContextLinesWithMaxMatches:
    """context_lines + max_matches interaction: only first match gets context."""

    @pytest.mark.asyncio
    async def test_context_lines_respects_max_matches(self, tmp_path):
        (tmp_path / "a.py").write_text("line1\nMATCH_A\nline3\n")
        (tmp_path / "b.py").write_text("line4\nMATCH_B\nline6\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "MATCH_", "context_lines": 1, "max_matches": 1}, ctx)
        assert result.metadata["matches"] == 1
        assert "MATCH_A" in result.display
        assert "MATCH_B" not in result.display
        # Context line for first match present
        assert "a.py-1-line1" in result.display or "a.py-3-line3" in result.display
