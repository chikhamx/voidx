"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file import FileReadInput, FileReplaceInput, WriteInput, FileReadTool, WriteTool, FileReplaceTool
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


    def test_write_input_supports_insert_and_append(self):
        insert = WriteInput(file_path="x.py", op="insert", lineno=3, new_string="added\n")
        append = WriteInput(file_path="x.py", op="append", new_string="appended\n")
        assert insert.lineno == 3
        assert append.op == "append"

    def test_write_insert_requires_lineno(self):
        with pytest.raises(ValueError):
            WriteInput(file_path="x.py", op="insert", new_string="nope\n")

    def test_write_append_ignores_lineno(self):
        inp = WriteInput(file_path="x.py", op="append", lineno=3, new_string="nope\n")
        assert inp.op == "append"
        assert inp.lineno == 3

    def test_write_schema_has_insert_append_fields(self):
        schema = WriteTool().parameters_schema()

        assert set(schema["properties"]) == {"file_path", "op", "lineno", "new_string"}
        assert "insert" in schema["properties"]["op"]["description"]
        assert "append" in schema["properties"]["op"]["description"]

    def test_replace_input_uses_bounds_without_operation(self):
        inp = FileReplaceInput(file_path="x.py", bounds=[{"line_no": 3, "anchor": "old"}, {"line_no": 5, "anchor": "tail"}], new_string="new")
        schema = FileReplaceTool().parameters_schema()

        assert inp.resolved_start_anchor == "old"
        assert inp.resolved_end_no == 5
        assert set(schema["properties"]) == {"file_path", "bounds", "new_string"}
        assert "Replacement boundary lines" in schema["properties"]["bounds"]["description"]
        assert "whole lines" in FileReplaceTool().description.lower()
        assert "operation" not in schema["properties"]
        assert "edits" not in schema["properties"]
        assert "old_text" not in schema["properties"]

    def test_replace_input_rejects_duplicate_two_bound_line_numbers(self):
        with pytest.raises(ValueError):
            FileReplaceInput(file_path="x.py", bounds=[{"line_no": 5, "anchor": "old"}, {"line_no": 5, "anchor": "tail"}], new_string="new")
        with pytest.raises(ValueError):
            FileReplaceInput(file_path="x.py", bounds=[{"line_no": 5, "anchor": ""}, {"line_no": 3, "anchor": "tail"}], new_string="new")

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
