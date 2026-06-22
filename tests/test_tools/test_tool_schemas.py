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
from voidx.tools.file_ops import FileReadInput, FileReplaceInput, FileInput, LineInput, FileReadTool, FileTool, LineTool, FileReplaceTool
from voidx.tools.file_ops.write import FileWriteInput, FileWriteTool
from voidx.tools.file_ops.edit_execute import FileEditInput, FileEditTool
from voidx.tools.file_ops.types import EditEntry
from voidx.tools.file_ops.edit_resolve import _find_paragraph
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
from voidx.agent.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
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



class TestToolSchemas:
    """Every tool has typed, validatable input."""

    def test_base_tool_requires_id_and_description(self):
        with pytest.raises(TypeError, match="must define"):
            class BadTool(BaseTool):
                def parameters_schema(self):
                    return {}
                async def execute(self, args, ctx):
                    pass

    def test_base_tool_subclass_with_id_and_description_ok(self):
        class GoodTool(BaseTool):
            id = "good"
            description = "a good tool"
            def parameters_schema(self):
                return {}
            async def execute(self, args, ctx):
                pass
        assert GoodTool.id == "good"

    def test_read_input_validates(self):
        inp = FileReadInput(file_path="foo.py")
        assert inp.file_path == "foo.py"
        assert inp.offset is None
        assert inp.limit is None

    def test_read_input_with_offset(self):
        inp = FileReadInput(file_path="foo.py", offset=10, limit=5)
        assert inp.offset == 10
        assert inp.limit == 5

    def test_read_input_rejects_zero_and_negative_values(self):
        with pytest.raises(ValueError):
            FileReadInput(file_path="foo.py", offset=0)
        with pytest.raises(ValueError):
            FileReadInput(file_path="foo.py", limit=0)
        with pytest.raises(ValueError):
            FileReadInput(file_path="foo.py", offset=-1)
        with pytest.raises(ValueError):
            FileReadInput(file_path="foo.py", limit=-2)

    def test_file_input_requires_dest_path_for_move(self):
        inp = FileInput(file_path="x.py", op="create")
        assert inp.file_path == "x.py"
        assert inp.op == "create"
        with pytest.raises(ValueError):
            FileInput(file_path="x.py", op="move")

    def test_file_schema_describes_file_operations(self):
        schema = FileTool().parameters_schema()
        assert set(schema["properties"]) == {"file_path", "op", "dest_path", "overwrite"}
        assert "create" in schema["properties"]["op"]["description"]
        assert "move" in schema["properties"]["dest_path"]["description"]

    def test_line_input_supports_insert_and_delete(self):
        insert = LineInput(file_path="x.py", op="insert", lineno=3, new_string="added\n")
        delete = LineInput(file_path="x.py", op="delete", lineno=3, end_no=5)
        assert insert.lineno == 3
        assert delete.end_no == 5

    def test_line_delete_rejects_invalid_line_range(self):
        with pytest.raises(ValueError):
            LineInput(file_path="x.py", op="delete", lineno=0)
        with pytest.raises(ValueError):
            LineInput(file_path="x.py", op="delete", lineno=5, end_no=3)

    def test_line_schema_has_combined_insert_delete_fields(self):
        schema = LineTool().parameters_schema()

        assert set(schema["properties"]) == {"file_path", "op", "lineno", "end_no", "new_string"}
        assert "insert" in schema["properties"]["op"]["description"]
        assert "delete" in schema["properties"]["op"]["description"]

    def test_replace_input_uses_start_end_line_range_without_operation(self):
        inp = FileReplaceInput(file_path="x.py", start_no=3, end_no=5, prefix="old", suffix="tail", new_string="new")
        schema = FileReplaceTool().parameters_schema()

        assert inp.prefix == "old"
        assert inp.end_no == 5
        assert set(schema["properties"]) == {"file_path", "start_no", "end_no", "prefix", "suffix", "new_string"}
        assert "exact first line" in schema["properties"]["start_no"]["description"].lower()
        assert "exact last line" in schema["properties"]["end_no"]["description"].lower()
        assert "first line" in schema["properties"]["prefix"]["description"].lower()
        assert "empty string" in schema["properties"]["prefix"]["description"].lower()
        assert "last line" in schema["properties"]["suffix"]["description"].lower()
        assert "empty string" in schema["properties"]["suffix"]["description"].lower()
        assert "whole lines" in FileReplaceTool().description.lower()
        assert "operation" not in schema["properties"]
        assert "edits" not in schema["properties"]
        assert "old_text" not in schema["properties"]

    def test_replace_input_rejects_reversed_line_range(self):
        with pytest.raises(ValueError):
            FileReplaceInput(file_path="x.py", start_no=5, end_no=3, prefix="old", suffix="tail", new_string="new")

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
        inp = AgentInput.model_validate({
            "agent": "voidx",
            "mode": "inspect",
            "task": "Inspect auth flow",
            "target": "src/voidx/auth.py",
        })
        assert inp.agent == "voidx"
        assert inp.mode == "inspect"
        assert inp.task == "Inspect auth flow"
        assert inp.target == "src/voidx/auth.py"
        assert inp.result_preset == "auto"
        schema = AgentInput.model_json_schema()
        assert "agent" in schema["properties"]
        assert "sub-voidx" not in str(schema)
        assert "mode" in schema["required"]
        assert "task" in schema["required"]
        assert "target" in schema["required"]
        assert "description" not in schema["required"]
        assert "goal_resolution" not in schema["required"]
        assert "result" not in schema["required"]
        assert "persona" not in schema["required"]
        assert "max_steps" not in schema["required"]
        assert "delegation_reason" not in schema["required"]
        assert "expected_output" not in schema["required"]
        assert "parent_evidence" not in schema["required"]
        assert "subagent_type" not in schema["properties"]

    def test_agent_input_requires_mode_task_and_target(self):
        with pytest.raises(ValueError):
            AgentInput.model_validate({"agent": "voidx", "task": "inspect"})
