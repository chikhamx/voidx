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
    FileInsertInput,
    FileReplaceInput,
    EditEntry,
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    FileInsertTool,
    FileReplaceTool,
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

    def test_edit_input(self):
        inp = FileEditInput(
            file_path="x.py",
            edits=[EditEntry(operation="replace", lineno=1, prefix="a", suffix="a", new_string="b")],
        )
        assert inp.file_path == "x.py"
        assert len(inp.edits) == 1
        assert inp.edits[0].operation == "replace"

    def test_edit_input_supports_single_insert_operation(self):
        inp = FileEditInput(
            file_path="x.py",
            edits=[EditEntry(operation="insert", lineno=0, prefix="", suffix="", new_string="header\n")],
        )
        assert inp.edits[0].operation == "insert"
        assert inp.edits[0].lineno == 0

    def test_edit_schema_describes_prefix_suffix_matching(self):
        schema = EditEntry.model_json_schema()
        assert "prefix" in schema["properties"]
        assert "suffix" in schema["properties"]
        assert "snippet" in schema["properties"]["prefix"]["description"].lower()
        assert "100" in schema["properties"]["lineno"]["description"]

    def test_find_paragraph_supports_multiline_snippets(self):
        lines = ["def f():", "    value = 1", "    return value", "", "def g():", "    pass"]

        assert _find_paragraph(lines, "replace", 2, "def f():\n    value", "return value") == (1, 3)

    def test_edit_input_requires_explicit_operation(self):
        with pytest.raises(ValueError):
            EditEntry(lineno=1, prefix="a", suffix="a", new_string="b")

    def test_insert_input_only_needs_line_and_content(self):
        inp = FileInsertInput(file_path="x.py", lineno=3, new_string="added\n")
        schema = FileInsertTool().parameters_schema()

        assert inp.lineno == 3
        assert set(schema["properties"]) == {"file_path", "lineno", "new_string"}

    def test_replace_input_uses_prefix_suffix_text_segment_without_operation(self):
        inp = FileReplaceInput(file_path="x.py", lineno=3, prefix="old", suffix="old", new_string="new")
        schema = FileReplaceTool().parameters_schema()

        assert inp.prefix == "old"
        assert set(schema["properties"]) == {"file_path", "lineno", "prefix", "suffix", "new_string"}
        assert "30" in schema["properties"]["lineno"]["description"]
        assert "text segment" in schema["properties"]["prefix"]["description"].lower()
        assert "included in the replaced text" in schema["properties"]["suffix"]["description"].lower()
        assert "do not use text you want to keep" in FileReplaceTool().description.lower()
        assert "operation" not in schema["properties"]
        assert "edits" not in schema["properties"]
        assert "old_text" not in schema["properties"]

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
