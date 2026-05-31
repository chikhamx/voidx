"""Smoke tests for tool system — types, execution, error handling."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext, ToolResult
from voidx.tools.file_ops import FileReadInput, FileWriteInput, FileEditInput, EditEntry
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.registry import ToolRegistry


class TestToolSchemas:
    """Every tool has typed, validatable input."""

    def test_read_input_validates(self):
        inp = FileReadInput(file_path="foo.py")
        assert inp.file_path == "foo.py"
        assert inp.offset is None
        assert inp.limit is None

    def test_read_input_with_offset(self):
        inp = FileReadInput(file_path="foo.py", offset=10, limit=5)
        assert inp.offset == 10
        assert inp.limit == 5

    def test_edit_input(self):
        inp = FileEditInput(file_path="x.py", edits=[EditEntry(old_string="a", new_string="b")])
        assert inp.file_path == "x.py"
        assert len(inp.edits) == 1

    def test_glob_input(self):
        inp = GlobInput(pattern="**/*.py")
        assert inp.pattern == "**/*.py"

    def test_grep_input(self):
        inp = GrepInput(pattern="TODO", include="*.py")
        assert inp.pattern == "TODO"

    def test_bash_input(self):
        inp = BashInput(command="ls")
        assert inp.command == "ls"
        assert inp.timeout == 120

    def test_agent_input_uses_child_agent_schema(self):
        assert AgentInput.model_validate({"agent": "explore", "description": "inspect"}).agent == "explore"
        schema = AgentInput.model_json_schema()
        assert "agent" in schema["properties"]
        assert "subagent_type" not in schema["properties"]


class TestToolRegistry:
    """Registry knows all tools."""

    def test_all_tools_registered(self):
        r = ToolRegistry()
        ids = r.ids()
        assert "read" in ids
        assert "write" in ids
        assert "edit" in ids
        assert "glob" in ids
        assert "grep" in ids
        assert "bash" in ids
        assert "repo_map" in ids
        assert "lsp_diagnostics" in ids
        assert "lsp_symbols" in ids
        assert "lsp_definition" in ids
        assert "lsp_references" in ids
        assert "lsp_format" in ids

    def test_tools_for_llm(self):
        r = ToolRegistry()
        tools = r.tools_for_llm()
        assert len(tools) == len(r.ids())
        assert len(tools) >= 10
        for t in tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]

    def test_unknown_tool(self):
        r = ToolRegistry()
        assert r.get("nonexistent") is None


class TestFileOps:
    """File operations work on real files."""

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

    @pytest.mark.asyncio
    async def test_write(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("write", {"file_path": "out.txt", "content": "hello"}, ctx)
        assert "File written" in result.output
        assert (tmp_path / "out.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_edit(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool(
            "edit",
            {"file_path": "edit.txt", "edits": [{"old_string": "hello", "new_string": "hi"}]},
            ctx,
        )
        assert "File edited" in result.output
        assert (tmp_path / "edit.txt").read_text() == "hi world"

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "nope.txt"}, ctx)
        assert "File not found" in result.output


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
        assert "a.py" in result.output
        assert "sub/b.py" in result.output.replace("\\", "/")

    @pytest.mark.asyncio
    async def test_grep(self, tmp_path):
        (tmp_path / "code.py").write_text("TODO: fix this\nprint('ok')\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert "code.py" in result.output
        assert "TODO" in result.output

    @pytest.mark.asyncio
    async def test_grep_no_match(self, tmp_path):
        (tmp_path / "code.py").write_text("nothing here\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "XYZNOTFOUND"}, ctx)
        assert "No matches" in result.output


class TestBash:
    """Bash commands execute and capture output."""

    @pytest.mark.asyncio
    async def test_bash_echo(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("bash", {"command": "echo hello"}, ctx)
        assert "hello" in result.output
        assert result.metadata["exit_code"] == 0


class TestTaskTracker:
    """TaskTracker reports worker-role progress."""

    def test_start_and_update(self):
        tracker = TaskTracker()
        tracker.start("t1", "implement", "write foo.py", max_steps=5)
        t = tracker.get("t1")
        assert t is not None
        assert t.status == "running"
        assert t.agent == "implement"

        tracker.update("t1", step=3, last_output="writing file...")
        t = tracker.get("t1")
        assert t.step == 3
        assert "writing file" in t.last_output

    def test_finish(self):
        tracker = TaskTracker()
        tracker.start("t2", "explore", "search")
        tracker.finish("t2", "completed")
        assert tracker.get("t2").status == "completed"

    def test_list_running(self):
        tracker = TaskTracker()
        tracker.start("a", "explore", "x")
        tracker.start("b", "implement", "y")
        tracker.finish("a", "completed")
        running = tracker.list_running()
        assert len(running) == 1
        assert running[0].id == "b"

    def test_format_status(self):
        tracker = TaskTracker()
        tracker.start("t1", "implement", "write foo.py", max_steps=5)
        tracker.update("t1", step=2, last_output="found target")
        output = tracker.format_status()
        assert "implement" in output
        assert "running" in output

    @pytest.mark.asyncio
    async def test_task_status_tool(self, tmp_path):
        tracker = TaskTracker()
        tracker.start("t1", "explore", "scan directory")
        tool = TaskStatusTool(tracker=tracker)
        ctx = ToolContext(workspace=str(tmp_path))

        result = await tool.execute({}, ctx)
        assert "explore" in result.output
        assert "running" in result.output

        result2 = await tool.execute({"task_id": "t1"}, ctx)
        assert "t1" in result2.output
